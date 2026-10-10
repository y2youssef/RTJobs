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

A board that waits for a person (Indeed's emailed code, a LinkedIn checkpoint)
does not hold the cycle: the parent closes the cycle without it and keeps it
running in the background ("detached"; it ends its run after the wait, see
JobBoard.finish_after_person_wait). While one waits, this process stays up
and runs the next cycles itself on the scheduler's grid, skipping that board,
because ofelia (no-overlap) skips every tick while the container runs.
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
from boards.naukrigulf import NaukriGulfBoard
from boards.base import report_run_checks
from core import browser, db, login_state, timing
from core.log import setup_logging
from config import (BOARD_TIME_BUDGET_SECONDS, BOARDS_PARALLEL, CHROME_DEBUG_PORT, DB_PATH,
                    ENRICHMENT_ENABLED, SCRAPE_INTERVAL_MINUTES)
from core import board_budget

logger = logging.getLogger(__name__)

_shutting_down = False
_children: list[subprocess.Popen] = []  # board processes of the running cycle + detached ones
# Boards still waiting for a person after their cycle closed:
# name -> {"proc", "started", "port", "batch"}.
_detached: dict[str, dict] = {}


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
    NaukriGulfBoard,
]


def _period() -> float:
    """Seconds between scheduler ticks."""
    return SCRAPE_INTERVAL_MINUTES * 60


def _missed_tick(launched: float, now: float, minutes: int | None = None) -> bool:
    """True when a scheduler tick fell inside this run.

    Ofelia's "*/N" ticks sit on epoch multiples of N minutes (N divides 60,
    enforced in config), so a tick passed iff the run crossed one.
    """
    period = minutes * 60 if minutes else _period()
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

    result = _cycle()
    if result is None:
        return 0
    try:
        # A board waiting for a person outlived its cycle: run the next
        # cycles here on the grid (ofelia skips its ticks while we run).
        while _detached and _wait_for_tick(launched):
            launched = time.time()
            result = 1 if (_cycle() or result) else 0
    finally:
        _stop_detached()  # normally nothing left; on a signal/crash, stop them

    if "--scheduled" in sys.argv and _missed_tick(launched, time.time()):
        _start_next_cycle_now()
    return result


def _cycle() -> int | None:
    """One scrape cycle under the scraper lock; None if another cycle holds it."""
    # Serialize manual runs too: a cycle must never close another live scrape.
    with open(DB_PATH + '.scraper.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info('Another scrape cycle is active')
            return None
        batch_id = db.start_scrape_batch()
        interrupted = True
        try:
            db.touch_worker('scraper', 'scraping')
            result = _run_boards()
            interrupted = False
        finally:
            db.finish_scrape_batch(batch_id, interrupted=interrupted)
            db.touch_worker('scraper', 'idle')
    return result


def _wait_for_tick(launched: float) -> bool:
    """Between cycles while a board waits for a person.

    True at the next scheduler tick (at once if the last cycle, started at
    `launched`, overran one); False as soon as every waiting board has
    finished, so this process can exit and ofelia takes over again.
    """
    _tend_detached()
    if not _detached:
        return False
    if _missed_tick(launched, time.time()):
        return True
    period = _period()
    next_tick = (time.time() // period + 1) * period
    logger.info("Still waiting for a person: %s. Next cycle at the %s tick (without them).",
                ", ".join(_detached), time.strftime("%H:%M:%S", time.localtime(next_tick)))
    while time.time() < next_tick:
        _tend_detached()
        if not _detached:
            return False
        time.sleep(0.5)
    return True


def _run_boards() -> int:
    """Run every enabled board of this cycle; non-zero if any failed."""
    enabled = [cls for cls in BOARDS if getattr(cls, "enabled", True)]
    for cls in BOARDS:
        if cls not in enabled:
            logger.info("Board '%s' is disabled — skipping.", cls.name)
    _tend_detached()
    for cls in enabled:
        if cls.name in _detached:
            logger.info("Board '%s' is still waiting for a person from an earlier cycle — skipping.", cls.name)
    enabled = [cls for cls in enabled if cls.name not in _detached]
    # Once, before any Chrome starts: with boards in parallel a per-board
    # `pkill -f chrome` would kill the other boards' browsers. A waiting
    # board's Chrome holds the page it waits on: spare it.
    browser.kill_stray_chrome(keep_ports=[entry["port"] for entry in _detached.values()])
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


# Grace beyond the budget for the child's own (in-process) timeout to stop
# it cleanly before the parent kills it from outside.
_KILL_GRACE_SECONDS = 60


def _budget_file(name: str) -> str:
    return f"{DB_PATH}.{name}.budget"


def _spawn_board(name: str, batch_id: int | None) -> subprocess.Popen:
    """Start one board as a child process attached to this cycle's batch.

    Each child logs to its own file (scraper-<board>.log next to LOG_FILE):
    rotating one file from several processes would lose lines.
    """
    env = dict(os.environ)
    env[board_budget.STATE_ENV] = _budget_file(name)
    try:
        os.remove(_budget_file(name))
    except OSError:
        pass
    if env.get("LOG_FILE"):
        base, ext = os.path.splitext(env["LOG_FILE"])
        env["LOG_FILE"] = f"{base}-{name}{ext or '.log'}"
    return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--board", name,
                             "--batch-id", str(batch_id)], env=env)


def _run_parallel(boards: list) -> bool:
    """All boards at once; wait for the slowest. Returns True if any failed.

    The cycle (and its single classifier request) closes once every child
    has exited or is waiting for a person (then detached: it keeps running
    and later cycles skip its board until it exits). Each child is bounded by
    BOARD_TIME_BUDGET_SECONDS except while it waits for a person. Signals are
    forwarded to the children, detached ones included.
    """
    batch = db.active_batch()
    _children[:] = [entry["proc"] for entry in _detached.values()]
    procs, started, timed_out = {}, {}, set()
    ports = {cls.name: CHROME_DEBUG_PORT + getattr(cls, "port_offset", 0) for cls in boards}
    completed = False
    try:
        for cls in boards:
            procs[cls.name] = _spawn_board(cls.name, batch)
            started[cls.name] = time.time()
            _children.append(procs[cls.name])
        logger.info("Running boards in parallel: %s", ", ".join(procs))
        limit = BOARD_TIME_BUDGET_SECONDS + _KILL_GRACE_SECONDS
        while any(proc.poll() is None and name not in _detached for name, proc in procs.items()):
            for name, proc in procs.items():
                if proc.poll() is not None or name in _detached or name in timed_out:
                    continue
                if board_budget.active_seconds(started[name], _budget_file(name)) > limit:
                    timed_out.add(name)
                    _kill_board(name, proc, ports[name], batch)
                elif board_budget.waiting_for_person(_budget_file(name)):
                    _detached[name] = {"proc": proc, "started": started[name], "port": ports[name], "batch": batch}
                    logger.info("[%s] Waiting for a person — closing the cycle without it; it keeps "
                                "waiting, and the next cycles run the other boards on schedule.", name)
            _tend_detached()  # earlier cycles' waiting boards: reap, enforce the budget
            time.sleep(0.2)
        completed = True
    finally:
        # On a signal or crash, stop everything (waiting boards too) at once,
        # within compose's 30s stop grace; normally only stragglers remain.
        stop = [proc for name, proc in procs.items()
                if proc.poll() is None and not (completed and name in _detached)]
        if not completed:
            stop += [entry["proc"] for entry in _detached.values() if entry["proc"].poll() is None]
        for proc in stop:
            proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 25
        for proc in stop:
            try:
                proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        _children[:] = [entry["proc"] for entry in _detached.values()]
    codes = {name: proc.returncode for name, proc in procs.items() if name not in _detached}
    if any(codes.values()):
        logger.warning("Board processes exited with %s", codes)
    return any(codes.values())


def _tend_detached():
    """Reap waiting boards that finished; stop one that blew its budget
    (the person wait itself is paused time and never counts)."""
    limit = BOARD_TIME_BUDGET_SECONDS + _KILL_GRACE_SECONDS
    for name, entry in list(_detached.items()):
        proc = entry["proc"]
        if proc.poll() is not None:
            logger.info("[%s] Finished its wait for a person (exit %s); back in the next cycle.",
                        name, proc.returncode)
        elif board_budget.active_seconds(entry["started"], _budget_file(name)) > limit:
            _kill_board(name, proc, entry["port"], entry["batch"])
        else:
            continue
        del _detached[name]
        if proc in _children:
            _children.remove(proc)


def _stop_detached():
    """Signal/crash path: a waiting board must not outlive this process
    (the container would stop and kill it without cleanup)."""
    live = [entry["proc"] for entry in _detached.values() if entry["proc"].poll() is None]
    for proc in live:
        proc.send_signal(signal.SIGTERM)
    deadline = time.monotonic() + 25
    for proc in live:
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    _detached.clear()
    _children.clear()


def _kill_board(name: str, proc: subprocess.Popen, port: int, batch: int | None):
    """Stop a board that blew its budget from outside, Chrome included.

    SIGTERM first (its handler cleans up when it gets the chance), then
    SIGKILL; its Chrome runs in its own process group, found by CDP port.
    """
    detail = (f"{name} exceeded its {BOARD_TIME_BUDGET_SECONDS}s time budget and was stopped by the "
              "cycle so the other boards' jobs are not held back; the next run retries.")
    logger.error("[%s] %s", name, detail)
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
    found = subprocess.run(["pgrep", "-f", "--", f"--remote-debugging-port={port}"],
                           capture_output=True, text=True).stdout.split()
    for pid in found:
        try:
            os.killpg(int(pid), signal.SIGKILL)  # browser leads its own group
        except (ProcessLookupError, PermissionError, ValueError):
            pass
    db.finish_stuck_runs(batch, name, "timeout", detail)
    report_run_checks(name, {"time_budget": (False, detail)})


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
