"""Indeed login flow: email -> Continue -> emailed code (via Telegram) -> verify.

Indeed is login-gated: anonymous sessions 403-redirect to
/account/login?branding=login-required&from=bot-detection-anonymous, and the
Cloudflare turnstile alone no longer gets a session through. This account
signs in with an emailed 6-digit code ("Sign in with a code instead" — no
password, no Google session in automation).

Telegram-assisted: when logged out, the run emails itself a fresh code by
driving the form, alerts the failure channel with a ForceReply prompt, and
holds the browser open up to INDEED_CODE_WAIT_SECONDS for the user to DM
the code to the bot (a reply to the prompt is matched first, any 6-digit
message is accepted as fallback). Login is one-time — the session persists
in the profile volume; re-login only happens on expiry, with a cooldown so
a missed code doesn't spam Gmail with a fresh code every 6 minutes.
"""

import logging
import re
import time

from config import (
    INDEED_CODE_WAIT_SECONDS,
    INDEED_EMAIL,
    INDEED_LOGIN_URL,
    TELEGRAM_FAILURE_CHAT_ID,
)
from core import db, markup, telegram
from core.human import human_wait, type_delay
from core.telegram import notify_failure

logger = logging.getLogger(__name__)

# URL fragments that mean "not logged in" after a navigation.
_LOGGED_OUT = re.compile(r"(secure\.indeed\.com/auth|/account/login)")
# Markers of a live logged-in homepage (verified against a real capture).
_LOGGED_IN_MARKERS = ('"isLoggedIn":true', "account/logout")
# Cloudflare interstitial markers (titles + body fragments). "Security Check"
# is Indeed's challenge-page title (distinct from "Just a moment"); only
# checked on auth pages, never on job content, so no false positives.
_CF_MARKERS = ("Just a moment", "Security Check", "Access Denied",
               "INDEED_CLOUDFLARE_STATIC_PAGE", "cf-chl")

# Episode state (login_state table, indeed-scoped so LinkedIn's keys are
# untouched). A missed/failed code sets a cooldown; the post-cooldown run
# starts a fresh episode with a fresh alert + fresh code.
AWAITING_KEY = "indeed_awaiting_code"
WINDOW_KEY = "indeed_code_window"
ALERT_MSG_KEY = "indeed_code_alert_msg_id"
ALERTED_KEY = "indeed_logout_alerted"
COOLDOWN_KEY = "indeed_code_cooldown_until"
_BLOCK_ALERTED_KEY = "indeed_block_alerted"

_CODE_WAIT = INDEED_CODE_WAIT_SECONDS
_CODE_COOLDOWN = 30 * 60  # s — quiet period after a missed/rejected code


def is_logged_out_url(url: str) -> bool:
    return bool(_LOGGED_OUT.search(url or ""))


def _on_challenge(title: str, html: str) -> bool:
    return any(m in title or m in html for m in _CF_MARKERS)


def _settle(page, what: str, timeout_s: int = 120) -> bool:
    """Wait out any Cloudflare interstitial. Returns False if still stuck."""
    for _ in range(max(1, timeout_s // 5)):
        try:
            title = page.title()
            if not _on_challenge(title, ""):
                return True
            # Title hit — confirm with body before burning the whole budget.
            html = page.content()
            if not _on_challenge(title, html):
                return True
        except Exception:
            pass
        logger.info(f"[indeed-login] Challenge during {what} — waiting...")
        try:
            page.wait_for_timeout(5000)
        except Exception:
            time.sleep(5)
    logger.warning(f"[indeed-login] Still challenged after {what}.")
    return False


def _wait_text_gone(page, text: str, timeout_s: int = 90) -> None:
    """Wait for SPA hydration placeholders (e.g. 'Loading...') to clear."""
    for _ in range(max(1, timeout_s // 2)):
        try:
            body = page.locator("body").first.text_content() or ""
        except Exception:
            body = ""
        if text not in body:
            break
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)


def _snapshot(page, kind: str):
    try:
        return markup.save_snapshot("indeed", kind, page.content())
    except Exception:
        return None


def _cooldown_active() -> bool:
    try:
        return time.time() < float(db.get_state(COOLDOWN_KEY, "0"))
    except (ValueError, TypeError):
        return False


def _set_cooldown() -> None:
    db.set_state(COOLDOWN_KEY, time.time() + _CODE_COOLDOWN)
    db.set_state(ALERTED_KEY, "0")  # post-cooldown run starts a fresh episode
    db.set_state(AWAITING_KEY, "0")


def clear_episode_flags() -> None:
    """Successful run — clear logout/block episode state."""
    for key in (AWAITING_KEY, WINDOW_KEY, ALERT_MSG_KEY, ALERTED_KEY,
                COOLDOWN_KEY, _BLOCK_ALERTED_KEY):
        db.set_state(key, "0")


def mark_blocked_page() -> None:
    """CF / WAF block page seen mid-scrape: alert once per episode, no loop."""
    if db.get_state(_BLOCK_ALERTED_KEY, "0") == "1":
        return
    db.set_state(_BLOCK_ALERTED_KEY, "1")
    notify_failure(
        "Indeed blocked page",
        "Search page came back as a Cloudflare/block page (snapshot saved)."
        " No jobs this run; will retry on the next tick.",
        hint="If it persists for hours, the IP may be flagged — check CF.",
    )


def mark_logged_out() -> None:
    """Mid-scrape logout (safety net): alert once per episode, no CF grind."""
    if db.get_state(ALERTED_KEY, "0") == "1":
        return
    db.set_state(ALERTED_KEY, "1")
    notify_failure(
        "Indeed session expired mid-scrape",
        "Redirected to the sign-in page while scraping. The next run will"
        " drive the email+code login and ask for the code here.",
    )


def _alert_for_code() -> tuple[int, int | None]:
    """Open the code window and prompt the user. Returns (window, msg_id)."""
    window = telegram.latest_update_id()
    db.set_state(WINDOW_KEY, window)
    db.set_state(AWAITING_KEY, "1")
    db.set_state(ALERTED_KEY, "1")

    username = telegram.get_bot_username()
    dm = f"@{username}" if username else "the bot"
    wait_min = max(1, _CODE_WAIT // 60)
    msg_id = telegram.send_login_prompt(
        TELEGRAM_FAILURE_CHAT_ID,
        "Indeed session expired — re-login needed\n\n"
        f"A sign-in code was emailed to {INDEED_EMAIL}. Reply HERE with"
        f" the 6-digit code (or DM it to {dm}) within ~{wait_min}"
        " minutes — I'm holding the browser open.",
    )
    db.set_state(ALERT_MSG_KEY, msg_id or 0)
    logger.info(f"[indeed-login] Code prompt sent (msg {msg_id}, window {window}).")
    return window, msg_id


def _wait_for_code(page, code_sel: str, window: int,
                   msg_id: int | None) -> str:
    """Hold until the user DMs the code (long-poll, ~2 req/min) or timeout."""
    deadline = time.time() + _CODE_WAIT
    while time.time() < deadline:
        try:
            if page.locator(code_sel).first.count() == 0:
                logger.warning("[indeed-login] Code field vanished — aborting wait.")
                return ""
        except Exception:
            pass
        code = telegram.poll_login_code(
            min_update_id=window, reply_to_msg_id=msg_id, timeout=30
        )
        if code:
            return code
    logger.warning("[indeed-login] No code arrived in time.")
    return ""


def _submit_code(page, sel: dict, code: str) -> bool:
    try:
        field = page.locator(sel["code_input"]).first
        field.click()
        field.fill("")
        field.type(code, delay=type_delay())
        human_wait(1, 2)
        try:
            btn = page.locator(sel["code_submit"]).first
            if btn.count() > 0 and btn.is_enabled():
                btn.click()
            else:
                field.press("Enter")
        except Exception:
            field.press("Enter")
    except Exception as e:
        logger.warning(f"[indeed-login] Code submit automation error: {e}")
        _snapshot(page, "login_failure")
        return False

    page.wait_for_timeout(8000)
    _settle(page, "post-code")

    # The code is accepted via a multi-hop OAuth dance (auth ->
    # postauthfunnel -> oauth/v2/authorize -> eg.indeed.com) that can take
    # tens of seconds. Wait until we actually LEAVE the auth page — judging
    # by the URL too early misreads a mid-flight redirect as a rejection.
    left_auth = False
    for _ in range(45):  # ~90s
        try:
            url = page.url
        except Exception:
            url = ""
        if url and not is_logged_out_url(url):
            left_auth = True
            break
        try:
            page.wait_for_timeout(2000)
        except Exception:
            time.sleep(2)
    try:
        logger.info(f"[indeed-login] After code: {page.url} / {page.title()!r}")
    except Exception:
        pass
    if not left_auth:
        logger.warning("[indeed-login] Code rejected or expired.")
        snapshot = _snapshot(page, "login_failure")
        notify_failure(
            "Indeed login code rejected",
            "The code was not accepted (wrong or expired). Cooling down —"
            " the next attempt will email a fresh code and ask again.",
            snapshot,
        )
        return False

    # Verify with the homepage: the auth cookie must survive a fresh load.
    try:
        page.goto("https://eg.indeed.com/")
        page.wait_for_timeout(6000)
        _settle(page, "verify-homepage")
        home = page.content()
    except Exception as e:
        logger.warning(f"[indeed-login] Verify navigation failed: {e}")
        return False
    if any(m in home for m in _LOGGED_IN_MARKERS):
        logger.info("[indeed-login] SUCCESS — session active.")
        return True
    logger.warning("[indeed-login] Verify failed — login markers missing.")
    _snapshot(page, "login_failure")
    return False


def _strip_jsessionid(url: str) -> str:
    """Drop the `;jsessionid=...` matrix param Indeed's servlet appends to
    the auth redirect. The dispatcher 400s on it when the session cookie
    hasn't round-tripped yet; re-requesting the clean URL with the now-set
    cookie lands on the real sign-in page (verified live)."""
    return re.sub(r";jsessionid=[^?]*", "", url or "")


def _land_on_signin(page, selectors: dict) -> bool:
    """Navigate to the login URL and end up on a usable sign-in form.

    ONE in-browser navigation (not the engine's redirect chain): the engine
    follows the 307/302 to secure.indeed.com as a separate fetch and
    Indeed's dispatcher 400s on the cookieless `;jsessionid` URL. The
    browser's own cookie jar handles it natively — with one retry on the
    stripped URL when the dispatcher still 400s (observed flaky live).
    """
    sel = selectors["login"]
    for attempt in (1, 2, 3):
        try:
            page.goto(INDEED_LOGIN_URL)
        except Exception as e:
            logger.warning(f"[indeed-login] Login navigation failed: {e}")
            return False
        if not _settle(page, "login landing"):
            _snapshot(page, "cloudflare_challenge")
            return False
        try:
            url = page.url
        except Exception:
            url = ""
        logger.info(f"[indeed-login] Landed (try {attempt}): {url} / {page.title()!r}")

        if not is_logged_out_url(url):
            logger.info("[indeed-login] Already logged in (no auth redirect).")
            return True

        clean = _strip_jsessionid(url)
        if clean != url:
            logger.info("[indeed-login] Retrying without ;jsessionid ...")
            try:
                page.goto(clean)
            except Exception as e:
                logger.warning(f"[indeed-login] Clean-URL retry failed: {e}")
                return False
            if not _settle(page, "clean-URL retry"):
                _snapshot(page, "cloudflare_challenge")
                return False

        try:
            page.locator(sel["email"]).first.wait_for(state="visible", timeout=20_000)
            return True
        except Exception:
            logger.info("[indeed-login] Email form not ready — retrying landing...")
    logger.warning("[indeed-login] Sign-in form never appeared.")
    _snapshot(page, "login_failure")
    return False


def _do_login(page, selectors: dict) -> bool:
    if not INDEED_EMAIL:
        notify_failure(
            "Indeed email missing",
            "Set INDEED_EMAIL in .env (the address Indeed emails codes to).",
        )
        return False

    sel = selectors["login"]
    if not _land_on_signin(page, selectors):
        return False

    try:
        email = page.locator(sel["email"]).first
        email.click()
        email.fill("")
        email.type(INDEED_EMAIL, delay=type_delay())
        human_wait(1, 2)
        page.locator(sel["email_submit"]).first.click()
    except Exception as e:
        logger.warning(f"[indeed-login] Email step automation error: {e}")
        _snapshot(page, "login_failure")
        return False

    _wait_text_gone(page, "Loading...")
    page.wait_for_timeout(2000)

    # This account offers Google one-tap (needs a Google session we don't
    # have) and "Sign in with a code instead" — take the code path.
    try:
        if page.locator(sel["code_fallback"]).count() == 0:
            logger.warning("[indeed-login] No code-fallback link (layout change?).")
            snapshot = _snapshot(page, "login_failure")
            notify_failure(
                "Indeed login layout changed",
                "Expected 'Sign in with a code instead' is missing —"
                " selectors need updating (snapshot saved).",
                snapshot,
            )
            return False
        logger.info("[indeed-login] Requesting emailed code...")
        page.locator(sel["code_fallback"]).first.click()
    except Exception as e:
        logger.warning(f"[indeed-login] Code-fallback click failed: {e}")
        _snapshot(page, "login_failure")
        return False

    try:
        page.locator(sel["code_input"]).first.wait_for(state="visible", timeout=60_000)
    except Exception:
        logger.warning("[indeed-login] Code field never appeared.")
        snapshot = _snapshot(page, "login_failure")
        notify_failure(
            "Indeed code field missing",
            "Clicked 'code instead' but no code field appeared (snapshot saved).",
            snapshot,
        )
        return False

    window, msg_id = _alert_for_code()
    code = _wait_for_code(page, sel["code_input"], window, msg_id)
    if not code:
        notify_failure(
            "Indeed login code missing",
            f"No code arrived within {_CODE_WAIT // 60} minutes. Cooling"
            " down — the next attempt will email a fresh code and ask again.",
        )
        _set_cooldown()
        return False

    if _submit_code(page, sel, code):
        clear_episode_flags()
        return True
    _set_cooldown()
    return False


def ensure_logged_in(page, selectors: dict) -> bool:
    """Entry point (used as page_action). Returns True if ready to scrape."""
    try:
        url = page.url
    except Exception:
        return False

    if not is_logged_out_url(url):
        return True

    if _cooldown_active():
        logger.info("[indeed-login] In code cooldown — skipping login this run.")
        return False

    logger.info("[indeed-login] Logged out — starting email+code login.")
    return _do_login(page, selectors)
