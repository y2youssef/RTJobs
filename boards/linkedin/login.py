"""LinkedIn login flow: session check, automated credential fill,
checkpoint/2FA handling with manual-solve pause, and retry/cooldown wiring."""

import logging
import os
import re
import shutil
import subprocess
import time

from config import (
    CHECKPOINT_WAIT_SECONDS,
    KILL_CHROME_ON_START,
    LINKEDIN_EMAIL,
    LINKEDIN_LOGIN_URL,
    LINKEDIN_PASSWORD,
    LINKEDIN_PROFILE_DIR,
)
from core import login_state, markup
from core.human import human_wait, type_delay
from core.telegram import notify_failure

logger = logging.getLogger(__name__)

SIGN_IN_BUTTON = re.compile(r"^(Sign in|تسجيل الدخول)$")

# URL fragments that indicate where we are after login attempts.
# NOTE: /feed only — logged-out guests get redirected to /jobs too, so
# matching /jobs here would treat a guest session as logged in.
_FEED_OK = re.compile(r"/feed")
_CHECKPOINT = re.compile(r"(checkpoint|security_verification|challenge)")
# Destinations that end the post-load redirect wait (never "login": the
# current URL already matches it).
_LANDED = re.compile(r"(/feed|checkpoint|security_verification|challenge)")


def kill_zombie_chrome():
    """Kill stray chrome processes (container safety net, opt-in).

    Stale profile lock cleanup lives in core.browser.launch_cdp_chrome
    (clean_locks=True), right before each launch.
    """
    if not KILL_CHROME_ON_START:
        return
    logger.info("[login] Killing stray Chrome processes...")
    try:
        subprocess.run(
            ["pkill", "-f", "chrome"], capture_output=True, timeout=10
        )
    except Exception:
        pass


def wipe_profile():
    """Delete the browser profile so the next login starts completely fresh."""
    if not os.path.isdir(LINKEDIN_PROFILE_DIR):
        return
    logger.info(f"[login] Wiping profile dir {LINKEDIN_PROFILE_DIR}")
    shutil.rmtree(LINKEDIN_PROFILE_DIR, ignore_errors=True)


def _on_checkpoint(url: str) -> bool:
    return bool(_CHECKPOINT.search(url))


def _is_logged_in(page) -> bool:
    return bool(_FEED_OK.search(page.url))


def _login_error_text(page, selectors: dict) -> str:
    """Text of a VISIBLE, non-empty sign-in error, else "".

    A rejected login now pauses all automatic logins, so hidden/empty error
    placeholders in the form markup must never count as a rejection.
    """
    try:
        for element in page.locator(selectors["login"]["error"]).all()[:5]:
            if element.is_visible():
                text = " ".join((element.text_content() or "").split())
                if text:
                    return text[:200]
    except Exception:
        pass
    return ""


def _wait_for_landing(page, timeout_ms: int = 8_000) -> None:
    """Let the /login -> /feed redirect of an active session settle.

    page_action runs right after DOMContentLoaded (scrapling's `wait` only
    starts afterwards), before LinkedIn routes an active session to /feed.
    Judging page.url that early mistook live sessions for logged-out ones:
    every saved login_failure snapshot (Sep-Oct 2026) is the Feed page.
    wait_until="commit": LinkedIn never fires `load` (Gotchas #1).
    """
    if _is_logged_in(page) or _on_checkpoint(page.url):
        return
    try:
        page.wait_for_url(_LANDED, timeout=timeout_ms, wait_until="commit")
    except Exception:
        pass  # Still on the sign-in form: a genuine logged-out session.


def _handle_checkpoint(page, selectors: dict) -> bool:
    """Security checkpoint / 2FA: alert, pause for manual solve via CDP."""
    logger.warning("[login] CHECKPOINT detected — pausing for manual solve.")
    snapshot = markup.save_snapshot("linkedin", "checkpoint", page.content())
    notify_failure(
        "LinkedIn checkpoint / 2FA",
        "Security checkpoint detected. Attach to the live browser via CDP"
        f" and solve it manually — I will keep waiting up to"
        f" {CHECKPOINT_WAIT_SECONDS // 60} minutes.",
        snapshot,
        hint="Attach: http://localhost:9222 (chrome://inspect)",
    )

    deadline = time.time() + CHECKPOINT_WAIT_SECONDS
    while time.time() < deadline:
        time.sleep(5)
        if _is_logged_in(page):
            logger.info("[login] Checkpoint solved — resuming.")
            return True

    logger.warning("[login] Checkpoint not solved in time — aborting run.")
    notify_failure(
        "LinkedIn checkpoint unresolved",
        "Manual solve timed out. The next scheduled run will retry"
        " (the profile is never wiped over a checkpoint).",
    )
    _register_failure("checkpoint", "checkpoint unresolved", snapshot)
    return False


def _register_failure(kind: str, reason: str, snapshot: str | None = None,
                      error_text: str = ""):
    """Record one failed login attempt; see core.login_state for the kinds."""
    if kind == "credentials":
        login_state.lock_credentials(LINKEDIN_EMAIL, LINKEDIN_PASSWORD)
        logger.warning(f"[login] {reason} — automatic logins paused until the credentials change.")
        missing = not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD
        notify_failure(
            "LinkedIn credentials missing" if missing else "LinkedIn rejected the login credentials",
            ("LINKEDIN_EMAIL / LINKEDIN_PASSWORD are not set and the saved session has expired."
             if missing else
             "LinkedIn showed a sign-in error for the configured account"
             + (f': "{error_text}"' if error_text else "") + ".")
            + "\n\nAutomatic logins are paused so repeated failed attempts cannot get the"
            " account restricted. They resume by themselves once the credentials change.",
            snapshot,
            hint="Fix LINKEDIN_EMAIL/LINKEDIN_PASSWORD in .env and recreate the scraper"
                 " (docker compose up -d scraper), or run: python main.py --reset-login",
        )
        return

    count = login_state.record_failure(checkpoint=(kind == "checkpoint"))
    logger.warning(f"[login] Failure ({reason}). Consecutive failures: {count}")

    if login_state.max_retries_reached() and not login_state.alert_already_sent():
        login_state.mark_alert_sent()
        cooldown = login_state.remaining_seconds()
        if login_state.wipe_pending():
            next_step = "After the cooldown the profile is wiped once and login retried from scratch."
        elif login_state.streak_had_checkpoint():
            next_step = ("Logins continue after each cooldown (max 30 min); the profile is not"
                         " wiped because a security checkpoint is involved.")
        else:
            next_step = ("Logins continue after each cooldown (max 30 min); the profile is not"
                         " wiped again because a fresh profile already failed.")
        notify_failure(
            "LinkedIn login failing repeatedly",
            f"{count} consecutive failures — scraping is now blocked for"
            f" ~{cooldown // 60} minutes. {next_step}\n\n"
            "Check whether LinkedIn needs a manual checkpoint solve.",
            snapshot,
        )


def _fail(page, kind: str, reason: str, error_text: str = "") -> bool:
    """Register a failure unless the session turned out to be active.

    Lets the DOM (or a late /feed redirect) settle first; returns True when
    the page landed on the feed after all, so callers can report success.
    """
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    if _is_logged_in(page):
        logger.info(f"[login] Session is active after all ({reason}) — not a failure.")
        login_state.reset_retries()
        return True
    try:
        snapshot = markup.save_snapshot("linkedin", "login_failure", page.content())
    except Exception:
        snapshot = None
    _register_failure(kind, reason, snapshot, error_text)
    return False


def _click_role_button(page, name_pattern: re.Pattern) -> bool:
    try:
        btn = page.get_by_role("button", name=name_pattern)
        if btn.count() == 0:
            return False
        btn.first.click()
        return True
    except Exception:
        return False


def _ensure_username_visible(page, selectors: dict):
    """Return the username input locator once visible, or None.

    LinkedIn's guest landing page (linkedin.com) shows a 'Sign in with email'
    button instead of the form, and the login page may also be two-step
    (email -> continue -> password). Try: wait -> click the button -> or
    navigate to /login directly.
    """
    username = page.locator(selectors["login"]["username"]).first

    try:
        username.wait_for(state="visible", timeout=10_000)
        return username
    except Exception:
        pass

    label = selectors["login"].get("sign_in_with_email", "Sign in with email")
    pattern = re.compile(rf"{re.escape(label)}|email", re.I)
    if _click_role_button(page, pattern):
        try:
            username.wait_for(state="visible", timeout=10_000)
            return username
        except Exception:
            pass

    logger.info("[login] Falling back to navigating to /login directly...")
    try:
        page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")
        username.wait_for(state="visible", timeout=15_000)
        return username
    except Exception:
        return None


def _do_login(page, selectors: dict) -> bool:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        # Locks like a rejection, so the alert is sent once, not every run.
        _register_failure("credentials", "credentials missing")
        return False

    logger.info("[login] Filling credentials...")
    try:
        user_input = _ensure_username_visible(page, selectors)
        if user_input is None:
            raise RuntimeError("username field never became visible")

        human_wait(1, 3)
        user_input.click()
        user_input.fill("")  # clear any pre-filled value, then type humanly
        user_input.type(LINKEDIN_EMAIL, delay=type_delay())
        human_wait(1, 2)

        # Two-step flow: email -> Continue -> password
        pass_input = page.locator(selectors["login"]["password"]).first
        try:
            pass_input.wait_for(state="visible", timeout=5_000)
        except Exception:
            cont_label = selectors["login"].get("continue", "Continue")
            _click_role_button(
                page, re.compile(rf"^{re.escape(cont_label)}$", re.I)
            )
            pass_input.wait_for(state="visible", timeout=10_000)

        pass_input.click()
        pass_input.fill("")
        pass_input.type(LINKEDIN_PASSWORD, delay=type_delay())
        human_wait(1, 3)

        login_btn = page.get_by_role("button", name=SIGN_IN_BUTTON).first
        login_btn.click()
    except Exception as e:
        logger.warning(f"[login] Input automation error: {e}")
        return _fail(page, "other", "automation error")

    return _verify_routing(page, selectors)


def _classify(page, selectors: dict) -> bool | None:
    """Classify the current page after login. Returns True (logged in),
    False (failed with a known reason), or None (still undecided)."""
    if _is_logged_in(page):
        logger.info("[login] SUCCESS — session active.")
        login_state.reset_retries()
        return True

    if _on_checkpoint(page.url):
        return _handle_checkpoint(page, selectors)

    error_text = _login_error_text(page, selectors)
    if error_text:
        logger.warning("[login] LinkedIn rejected the credentials.")
        return _fail(page, "credentials", "credentials rejected", error_text)

    return None


def _verify_routing(page, selectors: dict) -> bool:
    """Wait for the post-submit destination and decide success/failure.

    Uses Playwright's event-based navigation wait (page.wait_for_url) —
    internally it listens for navigation events, so no polling from us, and
    it handles SPA (pushState) navigations too.

    NOTE: do NOT include 'login' in the wait pattern — the current URL
    already contains it, so wait_for_url would return instantly before the
    navigation completes. wait_until="commit": the default waits for the
    `load` event LinkedIn never fires, which burned the whole timeout on
    every successful login.
    """
    logger.info("[login] Verifying routing after submit...")
    dest = re.compile(r"(feed|/jobs|checkpoint|security_verification|challenge)")

    # Two event-driven waits back-to-back: the error message usually appears
    # within the first window, a slow navigation gets caught by the second.
    for timeout in (15_000, 15_000):
        try:
            page.wait_for_url(dest, timeout=timeout, wait_until="commit")
        except Exception:
            pass  # TimeoutError — still on the login form

        outcome = _classify(page, selectors)
        if outcome is not None:
            return outcome

    logger.warning(f"[login] Unexpected landing page: {page.url}")
    return _fail(page, "other", f"unexpected landing: {page.url}")


def ensure_logged_in(page, selectors: dict) -> bool:
    """Entry point (used as page_action). Returns True if ready to scrape."""
    from core import timing

    if login_state.credentials_locked(LINKEDIN_EMAIL, LINKEDIN_PASSWORD):
        logger.info("[login] Configured credentials were rejected — not attempting a login.")
        return False

    if login_state.is_blocked():
        remaining = login_state.remaining_seconds()
        logger.info(f"[login] Blocked for another {remaining}s — skipping run.")
        return False

    _wait_for_landing(page)
    if _is_logged_in(page):
        logger.info("[login] Session already active.")
        login_state.reset_retries()
        return True

    if _on_checkpoint(page.url):
        with timing.stage("linkedin", "checkpoint_wait"):
            return _handle_checkpoint(page, selectors)

    logger.info("[login] Not logged in — starting automated login.")
    with timing.stage("linkedin", "login_flow"):
        return _do_login(page, selectors)
