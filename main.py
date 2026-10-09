"""RTJobs orchestrator: run every enabled job board, persist jobs, notify.

Scheduled runs (`--scheduled`, compose) are started by ofelia every
SCRAPE_INTERVAL_MINUTES with no-overlap. A cycle that runs past the next tick
made ofelia skip that tick, so it starts the next cycle immediately in a fresh
process instead of idling until the tick after (`_start_next_cycle_now`).

One cycle = one scrape batch = one classifier request. With BOARDS_PARALLEL
the boards run concurrently as child processes (`main.py --board NAME
--batch-id N`, each with its own Chrome, profile and CDP port), so the cycle
ends when the slowest board does. The parent owns the lock, the batch and
the overrun chaining; children only scrape their board into that batch.
"""

import fcntl
import logging
import os
import signal
import subprocess
import sys
import time

from boards.linkedin import LinkedInBoard
from boards.wuzzuf import WuzzufBoard
from boards.indeed import IndeedBoard
from boards.base import report_run_checks
from core import browser, db, login_state, timing
from core.log import setup_logging
from config import BOARDS_PARALLEL, DB_PATH, ENRICHMENT_ENABLED, SCRAPE_INTERVAL_MINUTES

logger = logging.getLogger(__name__)

_shutting_down = False
_children: list[subprocess.Popen] = []  # board processes of the running cycle


def _handle_term(signum, _frame):
    """First SIGTERM/SIGINT: raise SystemExit to unwind.

    Raising is the only way to break blocking waits (Playwright, checkpoint
    and login-code polling, Telegram long-polls); try/finally blocks then stop
    Chrome and close the run and cycle rows. SQLite transactions are atomic,
    so an interrupted write simply rolls back. A second signal means "stop
    now": kill our Chrome process groups (they would outlive us) and exit
    without running more cleanup.
    """
    global _shutting_down
    if _shutting_down:
        logger.warning("Second signal %s — forcing exit.", signum)
        for child in _children:  # their own second signal kills their Chrome
            if child.poll() is None:
                child.send_signal(signum)
        browser.kill_live_chrome()
        logging.shutdown()
        os._exit(128 + signum)
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


def _arg(name: str) -> str | None:
    return sys.argv[sys.argv.index(name) + 1] if name in sys.argv[:-1] else None


def main() -> int:
    launched = time.time()
    setup_logging()
    _install_signal_handlers()

    # Child of a parallel cycle: scrape one board into the parent's batch
    # (no lock, no batch bookkeeping, no migrations, no chaining).
    board_name = _arg("--board")
    if board_name:
        return _board_child(board_name, _arg("--batch-id"))

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
    """Run every enabled board of this cycle; non-zero if any failed."""
    enabled = [cls for cls in BOARDS if getattr(cls, "enabled", True)]
    for cls in BOARDS:
        if cls not in enabled:
            logger.info("Board '%s' is disabled — skipping.", cls.name)
    # Once, before any Chrome starts: with boards in parallel a per-board
    # `pkill -f chrome` would kill the other boards' browsers.
    browser.kill_stray_chrome()
    if BOARDS_PARALLEL and len(enabled) > 1:
        failed = _run_parallel(enabled)
    else:
        failed = False
        for cls in enabled:
            failed |= _run_one_board(cls)[1]
    batch = db.active_batch()
    with db.get_db() as conn:
        total_new = conn.execute("SELECT COALESCE(SUM(jobs_found), 0) FROM runs WHERE batch_id IS ?",
                                 (batch,)).fetchone()[0]
    logger.info("Done. New jobs this run: %s", total_new)
    if not ENRICHMENT_ENABLED:
        from core import telegram

        for cls in enabled:  # legacy single-channel delivery, after every Chrome closed
            sent = telegram.notify_jobs(db.get_unnotified(cls.name))
            logger.info("[%s] Notified %s job(s)", cls.name, sent)
    # Non-zero exit marks the run failed in `docker logs ofelia`.
    return 1 if failed else 0


def _run_one_board(board_cls) -> tuple[int, bool]:
    """One board in this process. Returns (new jobs, failed)."""
    try:
        board = board_cls()
    except (FileNotFoundError, ValueError) as e:
        # markup/ is bind-mounted and live-editable: a missing file or a
        # JSON typo skips only this board instead of the whole cycle.
        logger.error("Board '%s' selector config unusable: %s", board_cls.name, e)
        _record_config_health(board_cls.name, e)
        return 0, True
    _record_config_health(board.name, None)

    db.touch_worker("scraper", board.name)
    logger.info("Running board: %s", board.name)
    gained = 0  # the timing detail must not report another board's count
    try:
        with timing.stage("scraper", "board_run", lambda: {"board": board.name, "new": gained}):
            gained = board.run()
        return gained, False
    except SystemExit:
        # SIGTERM/SIGINT bubbled from _handle_term — the board's own
        # `finally: stop_chrome` + `finish_run` already ran.
        logger.warning("Interrupted during '%s' — exiting.", board.name)
        raise
    except Exception as e:
        # Escaped run() (e.g. Chrome could not start): once-per-episode alert.
        logger.exception("Board '%s' crashed: %s", board.name, e)
        report_run_checks(board.name, {"board_run": (False, f"Board '{board.name}' crashed: {e}"[:600])})
        if _shutting_down:
            raise SystemExit(0)
        return 0, True


def _spawn_board(name: str, batch_id: int | None) -> subprocess.Popen:
    """Start one board as a child process attached to this cycle's batch.

    Each child logs to its own file (scraper-<board>.log next to LOG_FILE):
    rotating one file from several processes would lose lines.
    """
    env = dict(os.environ)
    if env.get("LOG_FILE"):
        base, ext = os.path.splitext(env["LOG_FILE"])
        env["LOG_FILE"] = f"{base}-{name}{ext or '.log'}"
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--board", name,
                             "--batch-id", str(batch_id)], env=env)


def _run_parallel(boards: list) -> bool:
    """All boards at once; wait for the slowest. Returns True if any failed.

    The cycle (and its single classifier request) closes only after every
    child exits; each child is bounded by BOARD_TIME_BUDGET_SECONDS except
    while it waits for a person. Signals are forwarded to the children.
    """
    batch = db.active_batch()
    _children.clear()
    procs = {}
    try:
        for cls in boards:
            procs[cls.name] = _spawn_board(cls.name, batch)
            _children.append(procs[cls.name])
        logger.info("Running boards in parallel: %s", ", ".join(procs))
        while any(proc.poll() is None for proc in procs.values()):
            time.sleep(0.2)
    finally:
        for proc in procs.values():
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 25  # within compose's 30s stop grace
        for proc in procs.values():
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _children.clear()
    codes = {name: proc.returncode for name, proc in procs.items()}
    if any(codes.values()):
        logger.warning("Board processes exited with %s", codes)
    return any(codes.values())


def _board_child(name: str, batch_id: str | None) -> int:
    """Entry point of one board process inside a parallel cycle."""
    db.set_active_batch(int(batch_id) if batch_id and batch_id != "None" else None)
    board_cls = {cls.name: cls for cls in BOARDS}.get(name)
    if board_cls is None:
        logger.error("Unknown board %r", name)
        return 2
    return 1 if _run_one_board(board_cls)[1] else 0


def _record_config_health(name: str, error: Exception | None):
    """Selector-config health with the usual once-per-episode alert dedupe."""
    if error is None:
        report_run_checks(name, {"selectors_config": (True, "")})
        return
    run_id = db.start_run(name)
    db.finish_run(run_id, "config_error", error=f"{type(error).__name__}: {error}"[:500])
    report_run_checks(name, {"selectors_config": (
        False, f"markup/{name}/selectors.json is missing or not valid JSON "
               f"({type(error).__name__}: {error}). This board is skipped until it is fixed.")})


if __name__ == "__main__":
    sys.exit(main())
