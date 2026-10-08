"""RTJobs orchestrator: run every enabled job board, persist jobs, notify.

Scheduled runs (`--scheduled`, compose) are started by ofelia every
SCRAPE_INTERVAL_MINUTES with no-overlap. A cycle that runs past the next tick
made ofelia skip that tick, so it starts the next cycle immediately in a fresh
process instead of idling until the tick after (`_start_next_cycle_now`).
"""

import fcntl
import logging
import os
import signal
import sys
import time

from boards.linkedin import LinkedInBoard
from boards.wuzzuf import WuzzufBoard
from boards.indeed import IndeedBoard
from core import db, login_state, timing
from core.log import setup_logging
from config import DB_PATH, ENRICHMENT_ENABLED, SCRAPE_INTERVAL_MINUTES

logger = logging.getLogger(__name__)

_shutting_down = False


def _handle_term(signum, _frame):
    global _shutting_down
    if _shutting_down:
        logger.warning("Second signal %s, forcing exit.", signum)
        sys.exit(1)
    _shutting_down = True
    sig_name = signal.Signals(signum).name if hasattr(signal, "Signals") else str(signum)
    logger.warning("Received %s (%s) — shutting down gracefully...", sig_name, signum)
    # Raise to unwind the current board's `try/finally` so `stop_chrome`
    # and `db.finish_run` still execute.
    raise SystemExit(0)


def _install_signal_handlers():
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _handle_term)
        except (ValueError, OSError):
            # Not in main thread (e.g. pytest) — ignore.
            pass

BOARDS = [
    LinkedInBoard,
    WuzzufBoard,
    IndeedBoard,
]


def _missed_tick(launched: float, now: float, minutes: int = SCRAPE_INTERVAL_MINUTES) -> bool:
    """True when a scheduler tick fell inside this run.

    Ofelia's "*/N" ticks sit on epoch multiples of N minutes (N divides 60,
    enforced in config), so a tick passed iff the run crossed one.
    """
    period = minutes * 60
    return int(now // period) > int(launched // period)


def _start_next_cycle_now():
    """Replace this process with a fresh cycle (same PID, same container run).

    exec keeps ofelia's execution alive, so no-overlap keeps skipping ticks,
    while the new interpreter reloads blocklist/config like a normal start.
    The scraper lock was released when its file closed (fds are CLOEXEC).
    """
    logger.info("Cycle overran the %s-minute schedule — starting the next cycle now.",
                SCRAPE_INTERVAL_MINUTES)
    logging.shutdown()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, *sys.argv])


def main() -> int:
    launched = time.time()
    setup_logging()
    _install_signal_handlers()

    db.init_db()

    # Manual recovery: clear the LinkedIn retry/cooldown state and any
    # rejected-credentials lock.   python main.py --reset-login
    if "--reset-login" in sys.argv:
        login_state.reset_retries()
        logger.info("LinkedIn login state reset (retries, cooldown, credential lock) — next run will attempt login.")
        return 0

    # Anchor for container cold-start latency: the gap between ofelia's
    # grid and this entry covers docker start + xvfb + imports.
    timing.record("scraper", "process_start", 0)

    # Serialize manual runs too: a cycle must never close another live scrape.
    with open(DB_PATH + '.scraper.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info('Another scrape cycle is active')
            return 0
        batch_id = db.start_scrape_batch()
        interrupted = True
        try:
            db.touch_worker('scraper', 'scraping')
            result = _run_boards()
            interrupted = False
        finally:
            db.finish_scrape_batch(batch_id, interrupted=interrupted)
            db.touch_worker('scraper', 'idle')

    if "--scheduled" in sys.argv and _missed_tick(launched, time.time()):
        _start_next_cycle_now()
    return result


def _run_boards() -> int:
    total_new = 0
    failed = False
    for board_cls in BOARDS:
        # Check the class attribute FIRST so a disabled board can never
        # abort startup over a missing selectors.json.
        if not getattr(board_cls, "enabled", True):
            logger.info("Board '%s' is disabled — skipping.", board_cls.name)
            continue

        try:
            board = board_cls()
        except (FileNotFoundError, ValueError) as e:
            # markup/ is bind-mounted and live-editable: a missing file or a
            # JSON typo skips only this board instead of the whole cycle.
            logger.error("Board '%s' selector config unusable: %s", board_cls.name, e)
            _record_config_health(board_cls.name, e)
            failed = True
            continue
        _record_config_health(board.name, None)

        db.touch_worker("scraper", board.name)
        logger.info("Running board: %s", board.name)
        gained = 0  # the timing detail must not report the previous board's count
        try:
            with timing.stage("scraper", "board_run", lambda: {"board": board.name, "new": gained}):
                gained = board.run()
            total_new += gained
            if not ENRICHMENT_ENABLED:
                from core import telegram

                sent = telegram.notify_jobs(db.get_unnotified(board.name))
                logger.info("[%s] Notified %s job(s)", board.name, sent)
        except SystemExit:
            # SIGTERM/SIGINT bubbled from _handle_term — board's own
            # `finally: stop_chrome` + `finish_run` already ran; stop here
            # so Docker's 10s grace isn't wasted on the next board.
            logger.warning("Interrupted during '%s' — exiting.", board.name)
            raise
        except Exception as e:
            logger.exception("Board '%s' crashed: %s", board.name, e)
            from core import telegram

            telegram.notify_failure(f"Board '{board.name}' crashed", str(e))
            failed = True
            if _shutting_down:
                raise SystemExit(0)

    logger.info("Done. New jobs this run: %s", total_new)

    # Non-zero exit marks the run failed in `docker logs ofelia`.
    return 1 if failed else 0


def _record_config_health(name: str, error: Exception | None):
    """Selector-config health with the usual once-per-episode alert dedupe."""
    from core.scrape_health import ScrapeHealth

    health = ScrapeHealth(name)
    if error is None:
        health.check("selectors_config", True)
    else:
        run_id = db.start_run(name)
        db.finish_run(run_id, "config_error", error=f"{type(error).__name__}: {error}"[:500])
        health.check("selectors_config", False,
                     f"markup/{name}/selectors.json is missing or not valid JSON "
                     f"({type(error).__name__}: {error}). This board is skipped until it is fixed.")
    health.report(require_search=False)


if __name__ == "__main__":
    sys.exit(main())
