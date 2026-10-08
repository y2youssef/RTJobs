"""RTJobs orchestrator: run every enabled job board, persist jobs, notify."""

import fcntl
import logging
import signal
import sys

from boards.linkedin import LinkedInBoard
from boards.wuzzuf import WuzzufBoard
from boards.indeed import IndeedBoard
from core import db, login_state, timing
from core.log import setup_logging
from config import DB_PATH, ENRICHMENT_ENABLED

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


def main() -> int:
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
    # 6-minute grid and this entry covers docker start + xvfb + imports.
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
            return result
        finally:
            db.finish_scrape_batch(batch_id, interrupted=interrupted)
            db.touch_worker('scraper', 'idle')


def _run_boards() -> int:
    total_new = 0
    for board_cls in BOARDS:
        # Check the class attribute FIRST so a disabled board can never
        # abort startup over a missing selectors.json.
        if not getattr(board_cls, "enabled", True):
            logger.info("Board '%s' is disabled — skipping.", board_cls.name)
            continue

        try:
            board = board_cls()
        except FileNotFoundError as e:
            logger.error("Board config missing: %s", e)
            from core import telegram

            telegram.notify_failure(f"{board_cls.name}: selector configuration missing", str(e),
                                    hint="Restore markup/<board>/selectors.json before the next run.")
            sys.exit(1)

        db.touch_worker("scraper", board.name)
        logger.info("Running board: %s", board.name)
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
            if _shutting_down:
                raise SystemExit(0)

    logger.info("Done. New jobs this run: %s", total_new)

    return 0


if __name__ == "__main__":
    sys.exit(main())
