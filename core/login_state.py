"""LinkedIn login retry/cooldown state, backed by the SQLite login_state table.

Failure kinds decide what later runs may do:
- credentials: LinkedIn rejected (or we lack) the configured email/password.
  Retrying the same credentials cannot succeed and repeated failed logins
  risk an account restriction, so logins stop until the credentials change
  (detected through a salted fingerprint, never the password itself) or
  `python main.py --reset-login`.
- checkpoint / alert: a security check was not solved in time, or LinkedIn
  showed a page-level sign-in alert ("unusual activity, try later").
  Escalating cooldowns keep retrying, but the profile is never wiped: a fresh
  device makes LinkedIn more suspicious, not less.
- other (automation error, unexpected landing): escalating cooldowns, then ONE
  profile wipe per failure streak; if that does not help, cooldowns only.
A successful login (or --reset-login) ends the streak and clears everything.
"""

import hashlib
import hmac
import os
import time

from config import LOGIN_COOLDOWN_SECONDS, MAX_LOGIN_RETRIES
from core import db

RETRY_KEY = "retry_count"
BLOCKED_KEY = "blocked_until"  # epoch seconds
ALERTED_KEY = "max_retries_alerted"
CHECKPOINT_KEY = "streak_had_checkpoint"
WIPED_KEY = "streak_profile_wiped"
# "pbkdf2_sha256$<salt hex>$<digest hex>" of the rejected email+password, or "".
REJECTED_KEY = "rejected_credentials"


def get_retry_count() -> int:
    return int(db.get_state(RETRY_KEY, "0"))


def record_failure(checkpoint: bool = False) -> int:
    """Increment the consecutive-failure counter and set a cooldown."""
    count = get_retry_count() + 1
    db.set_state(RETRY_KEY, count)
    cooldown = LOGIN_COOLDOWN_SECONDS[min(count - 1, len(LOGIN_COOLDOWN_SECONDS) - 1)]
    db.set_state(BLOCKED_KEY, int(time.time()) + cooldown)
    if checkpoint:
        db.set_state(CHECKPOINT_KEY, "1")
    return count


def reset_retries():
    for key in (RETRY_KEY, BLOCKED_KEY, ALERTED_KEY, CHECKPOINT_KEY, WIPED_KEY):
        db.set_state(key, "0")
    db.set_state(REJECTED_KEY, "")


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


def streak_had_checkpoint() -> bool:
    return db.get_state(CHECKPOINT_KEY, "0") == "1"


def wipe_pending() -> bool:
    """This streak may still earn its one profile wipe (no checkpoint, no wipe yet)."""
    return not streak_had_checkpoint() and db.get_state(WIPED_KEY, "0") != "1"


def should_wipe_profile() -> bool:
    """Max retries reached, cooldown expired, and the streak may still wipe."""
    return max_retries_reached() and not is_blocked() and wipe_pending()


def mark_profile_wiped():
    """Give the fresh profile a full retry budget; never wipe again this streak."""
    for key in (RETRY_KEY, BLOCKED_KEY, ALERTED_KEY):
        db.set_state(key, "0")
    db.set_state(WIPED_KEY, "1")


def _fingerprint(email: str, password: str, salt: bytes) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", f"{email}\0{password}".encode(), salt, 200_000)
    return f"pbkdf2_sha256${salt.hex()}${digest.hex()}"


def lock_credentials(email: str, password: str):
    """Stop automatic logins until these exact credentials are replaced."""
    db.set_state(REJECTED_KEY, _fingerprint(email, password, os.urandom(16)))


def credentials_locked(email: str, password: str) -> bool:
    """True while the credentials LinkedIn rejected are still configured.

    Changing LINKEDIN_EMAIL/LINKEDIN_PASSWORD releases the lock and the
    failure streak automatically, so a fixed .env needs no manual reset.
    """
    stored = db.get_state(REJECTED_KEY, "")
    if not stored:
        return False
    try:
        salt = bytes.fromhex(stored.split("$")[1])
        if hmac.compare_digest(stored, _fingerprint(email, password, salt)):
            return True
    except (IndexError, ValueError):
        pass
    reset_retries()
    return False
