"""Per-board wall-clock budget, so one site cannot hold the whole cycle.

Two layers. Inside the board process a SIGALRM timer raises BoardTimeout for
a clean stop. That is best effort: Playwright's event dispatcher catches
BaseException from listeners and only re-raises it at the NEXT API call, so a
board stuck in a long wait (e.g. Cloudflare's solver) may never see it. The
parent process (main.py) therefore also enforces the budget from outside and
kills the board and its Chrome; it reads the paused time from the state file
named by RTJOBS_BUDGET_FILE, which paused() keeps current.

Every board's jobs wait for the slowest board (one classifier request per
cycle), and nothing else bounded a board's total time: on 2026-10-09 an
Indeed run spent 37 minutes in repeated Cloudflare Turnstile solves and
120s navigation timeouts. The budget is a SIGALRM timer in the board's main
thread; BoardTimeout derives from BaseException so the many
`except Exception` guards in browser code cannot swallow it. Waits for a
person (LinkedIn checkpoint, Indeed emailed code) run inside paused().
"""

from contextlib import contextmanager
import json
import os
import signal
import time

STATE_ENV = "RTJOBS_BUDGET_FILE"
_paused_total = 0.0


def _write_state(paused_since: float | None):
    path = os.environ.get(STATE_ENV)
    if not path:
        return
    try:
        with open(path, "w") as state:
            json.dump({"paused_total": _paused_total, "paused_since": paused_since}, state)
    except OSError:
        pass


def active_seconds(started: float, state_path: str, now: float | None = None) -> float:
    """Parent side: wall time since `started` minus the child's paused time."""
    now = time.time() if now is None else now
    try:
        with open(state_path) as handle:
            state = json.load(handle)
    except (OSError, ValueError):
        state = {}
    paused = float(state.get("paused_total") or 0)
    if state.get("paused_since"):
        paused += max(0.0, now - float(state["paused_since"]))
    return (now - started) - paused


class BoardTimeout(BaseException):
    """The board exceeded BOARD_TIME_BUDGET_SECONDS (excluding paused waits)."""


def _expire(_signum, _frame):
    raise BoardTimeout()


@contextmanager
def time_budget(seconds: float):
    previous = signal.signal(signal.SIGALRM, _expire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


@contextmanager
def paused():
    """Stop the clock while waiting for a person (2FA solve, emailed code)."""
    global _paused_total
    remaining, _ = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, 0)
    began = time.time()
    _write_state(began)
    try:
        yield
    finally:
        _paused_total += time.time() - began
        _write_state(None)
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, max(remaining, 1.0))
