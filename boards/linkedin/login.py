"""LinkedIn login flow: session check, automated credential fill,
checkpoint/2FA handling with manual-solve pause, and retry/cooldown wiring."""

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

SIGN_IN_BUTTON = re.compile(r"^(Sign in|تسجيل الدخول)$")

# URL fragments that indicate where we are after login attempts
_FEED_OK = re.compile(r"(feed|/jobs)")
_CHECKPOINT = re.compile(r"(checkpoint|security_verification|challenge)")


def kill_zombie_chrome():
    """Kill stray chrome processes (container safety net, opt-in)."""
    if not KILL_CHROME_ON_START:
        return
    print("[login] Killing stray Chrome processes...")
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
    print(f"[login] Wiping profile dir {LINKEDIN_PROFILE_DIR}")
    shutil.rmtree(LINKEDIN_PROFILE_DIR, ignore_errors=True)


def _on_checkpoint(url: str) -> bool:
    return bool(_CHECKPOINT.search(url))


def _is_logged_in(page) -> bool:
    return bool(_FEED_OK.search(page.url))


def _login_error_visible(page, selectors: dict) -> bool:
    try:
        return page.locator(selectors["login"]["error"]).count() > 0
    except Exception:
        return False


def _handle_checkpoint(page, selectors: dict) -> bool:
    """Security checkpoint / 2FA: alert, pause for manual solve via CDP."""
    print("[login] CHECKPOINT detected — pausing for manual solve.")
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
            print("[login] Checkpoint solved — resuming.")
            return True

    print("[login] Checkpoint not solved in time — aborting run.")
    notify_failure(
        "LinkedIn checkpoint unresolved",
        "Manual solve timed out. The next scheduled run will retry.",
    )
    _register_failure("checkpoint unresolved", snapshot)
    return False


def _register_failure(reason: str, snapshot: str | None = None):
    count = login_state.record_failure()
    print(f"[login] Failure ({reason}). Consecutive failures: {count}")

    if login_state.max_retries_reached() and not login_state.alert_already_sent():
        login_state.mark_alert_sent()
        cooldown = login_state.remaining_seconds()
        notify_failure(
            "LinkedIn login failing repeatedly",
            f"{count} consecutive failures — scraping is now blocked for"
            f" ~{cooldown // 60} minutes. After the cooldown the profile will"
            " be wiped and login retried from scratch.\n\n"
            "Check credentials or whether LinkedIn triggered a checkpoint.",
            snapshot,
        )


def _capture_failure(page) -> str | None:
    """Snapshot the page for debugging, letting the DOM settle first."""
    try:
        page.wait_for_timeout(1500)
    except Exception:
        pass
    try:
        return markup.save_snapshot("linkedin", "login_failure", page.content())
    except Exception:
        return None


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

    print("[login] Falling back to navigating to /login directly...")
    try:
        page.goto(LINKEDIN_LOGIN_URL, wait_until="domcontentloaded")
        username.wait_for(state="visible", timeout=15_000)
        return username
    except Exception:
        return None


def _do_login(page, selectors: dict) -> bool:
    if not LINKEDIN_EMAIL or not LINKEDIN_PASSWORD:
        notify_failure(
            "LinkedIn credentials missing",
            "Set LINKEDIN_EMAIL / LINKEDIN_PASSWORD in .env.",
        )
        return False

    print("[login] Filling credentials...")
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
        print(f"[login] Input automation error: {e}")
        snapshot = _capture_failure(page)
        _register_failure("automation error", snapshot)
        return False

    return _verify_routing(page, selectors)


def _classify(page, selectors: dict) -> bool | None:
    """Classify the current page after login. Returns True (logged in),
    False (failed with a known reason), or None (still undecided)."""
    if _is_logged_in(page):
        print("[login] SUCCESS — session active.")
        login_state.reset_retries()
        return True

    if _on_checkpoint(page.url):
        return _handle_checkpoint(page, selectors)

    if _login_error_visible(page, selectors):
        print("[login] Invalid credentials.")
        snapshot = _capture_failure(page)
        _register_failure("invalid credentials", snapshot)
        return False

    return None


def _verify_routing(page, selectors: dict) -> bool:
    """Wait for the post-submit destination and decide success/failure.

    Uses Playwright's event-based navigation wait (page.wait_for_url) —
    internally it listens for navigation events, so no polling from us, and
    it handles SPA (pushState) navigations too.

    NOTE: do NOT include 'login' in the wait pattern — the current URL
    already contains it, so wait_for_url would return instantly before the
    navigation completes.
    """
    print("[login] Verifying routing after submit...")
    dest = re.compile(r"(feed|/jobs|checkpoint|security_verification)")

    # Two event-driven waits back-to-back: the error message usually appears
    # within the first window, a slow navigation gets caught by the second.
    for timeout in (15_000, 15_000):
        try:
            page.wait_for_url(dest, timeout=timeout)
        except Exception:
            pass  # TimeoutError — still on the login form

        outcome = _classify(page, selectors)
        if outcome is not None:
            return outcome

    print(f"[login] Unexpected landing page: {page.url}")
    snapshot = _capture_failure(page)
    _register_failure(f"unexpected landing: {page.url}", snapshot)
    return False


def ensure_logged_in(page, selectors: dict) -> bool:
    """Entry point (used as page_action). Returns True if ready to scrape."""
    kill_zombie_chrome()

    if login_state.is_blocked():
        remaining = login_state.remaining_seconds()
        print(f"[login] Blocked for another {remaining}s — skipping run.")
        return False

    if _is_logged_in(page):
        print("[login] Session already active.")
        login_state.reset_retries()
        return True

    if _on_checkpoint(page.url):
        return _handle_checkpoint(page, selectors)

    print("[login] Not logged in — starting automated login.")
    return _do_login(page, selectors)
