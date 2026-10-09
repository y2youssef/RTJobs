"""Per-board wall-clock budget, so one site cannot hold the whole cycle.

Every board's jobs wait for the slowest board (one classifier request per
cycle), and nothing else bounded a board's total time: on 2026-10-09 an
Indeed run spent 37 minutes in repeated Cloudflare Turnstile solves and
120s navigation timeouts. The budget is a SIGALRM timer in the board's main
thread; BoardTimeout derives from BaseException so the many
`except Exception` guards in browser code cannot swallow it. Waits for a
person (LinkedIn checkpoint, Indeed emailed code) run inside paused().
"""

from contextlib import contextmanager
import signal


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
    remaining, _ = signal.getitimer(signal.ITIMER_REAL)
    signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        yield
    finally:
        if remaining > 0:
            signal.setitimer(signal.ITIMER_REAL, max(remaining, 1.0))
