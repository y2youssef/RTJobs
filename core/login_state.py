"""Login retry/cooldown state, backed by the SQLite login_state table."""

import time

from config import LOGIN_COOLDOWN_SECONDS, MAX_LOGIN_RETRIES
from core import db

RETRY_KEY = "retry_count"
BLOCKED_KEY = "blocked_until"  # epoch seconds
ALERTED_KEY = "max_retries_alerted"


def get_retry_count() -> int:
    return int(db.get_state(RETRY_KEY, "0"))


def record_failure() -> int:
    """Increment the consecutive-failure counter and set a cooldown."""
    count = get_retry_count() + 1
    db.set_state(RETRY_KEY, count)
    cooldown = LOGIN_COOLDOWN_SECONDS[min(count - 1, len(LOGIN_COOLDOWN_SECONDS) - 1)]
    db.set_state(BLOCKED_KEY, int(time.time()) + cooldown)
    return count


def reset_retries():
    db.set_state(RETRY_KEY, "0")
    db.set_state(BLOCKED_KEY, "0")
    db.set_state(ALERTED_KEY, "0")


def max_retries_reached() -> bool:
    return get_retry_count() >= MAX_LOGIN_RETRIES


def is_blocked() -> bool:
    return time.time() < get_blocked_until()


def get_blocked_until() -> float:
    return float(db.get_state(BLOCKED_KEY, "0"))


def remaining_seconds() -> int:
    return max(0, int(get_blocked_until() - time.time()))


def alert_already_sent() -> bool:
    return db.get_state(ALERTED_KEY, "0") == "1"


def mark_alert_sent():
    db.set_state(ALERTED_KEY, "1")


def should_wipe_profile() -> bool:
    """Reached max retries and cooldown has expired -> do a fresh login."""
    return max_retries_reached() and not is_blocked()
