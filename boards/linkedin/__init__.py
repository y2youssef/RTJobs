"""LinkedIn job board: login + scrape orchestration.

Chrome lifecycle: we launch ONE real Chrome with an HTTP DevTools endpoint
(CDP) so the session can be attached live from the host (localhost:9222) for
debugging and manual 2FA/checkpoint solves. The login session and the scrape
spider both connect to that same Chrome over cdp_url and reuse its default
(persistent profile) context, so the login cookies carry into the scrape.
"""

import logging
from scrapling.fetchers import StealthySession

from boards.base import JobBoard
from boards.linkedin import login, scraper
from config import (
    LINKEDIN_EMAIL,
    LINKEDIN_ENABLED,
    LINKEDIN_LOGIN_URL,
    LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS,
    LINKEDIN_PASSWORD,
    LINKEDIN_PROFILE_DIR,
)
from core import login_state, telegram, timing
from core.browser import patch_no_load_wait
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)


class LinkedInBoard(JobBoard):
    name = "linkedin"
    title = "LinkedIn"
    enabled = LINKEDIN_ENABLED
    profile_dir = LINKEDIN_PROFILE_DIR

    def before_browser(self) -> str | None:
        # LinkedIn rejected these credentials (or none are set): retrying them
        # can only get the account restricted. Changing them unlocks this.
        if login_state.credentials_locked(LINKEDIN_EMAIL, LINKEDIN_PASSWORD):
            logger.warning("[linkedin] Skipping run — the configured credentials were rejected."
                           " Update LINKEDIN_EMAIL/LINKEDIN_PASSWORD or run --reset-login.")
            return "credentials_rejected"

        # Cooldown active — don't even start the browser. (Also re-checked
        # inside page_action as a safety net.)
        if login_state.is_blocked():
            logger.info(f"[linkedin] Skipping run — blocked for {login_state.remaining_seconds()}s.")
            return "blocked"

        # Maxed retries + cooldown expired -> one fresh login per failure
        # streak (never after a checkpoint). The profile must be wiped BEFORE
        # the browser starts (never from under a live Chrome).
        if login_state.should_wipe_profile():
            logger.info("[linkedin] Cooldown over, retry count maxed — wiping profile once.")
            login.wipe_profile()
            login_state.mark_profile_wiped()
        # Stray-Chrome cleanup runs once per cycle in main.py, before any
        # board starts (boards run in parallel; see core.browser.kill_stray_chrome).
        return None

    def scrape(self, cdp: str, record) -> int:
        outcome: dict = {"ok": False}

        def page_action(page):
            outcome["ok"] = login.ensure_logged_in(page, self.selectors)

        with StealthySession(
            cdp_url=cdp,
            # No resource blocking: interception disables Chrome's HTTP cache
            # (see LinkedInJobSpider.configure_sessions).
            disable_resources=False,
            timeout=30_000,
            retry_delay=LINKEDIN_NAVIGATION_RETRY_DELAY_SECONDS,
            page_setup=patch_no_load_wait,
            page_action=page_action,
        ) as session:
            logger.info("[linkedin] Opening login page (redirects to feed if active)")
            with timing.stage("linkedin", "login_check"):
                # wait=0: scrapling's wait runs AFTER page_action, which already
                # waits for the feed redirect — it only added 5s idle per run.
                session.fetch(LINKEDIN_LOGIN_URL, wait=0)

        if not outcome["ok"]:
            logger.info("[linkedin] Login check failed — skipping scrape.")
            record.finish("login_failed")
            return 0

        health = ScrapeHealth(self.name)
        with timing.stage("linkedin", "scrape_spider",
                          lambda: {"items": len(result["items"]), "status": health.status}):
            result = scraper.scrape(self.selectors, cdp_url=cdp, health=health)

        if result["login_redirect"]:
            logger.info("[linkedin] Session died mid-scrape — aborting.")
            record.finish("session_expired")
            telegram.notify_failure(
                "LinkedIn session expired mid-scrape",
                "The browser was redirected to login while scraping."
                " The next run will re-login.",
                hint="Check http://localhost:9222 if it persists",
            )
            return 0

        return self.finish_scrape(record, health, result["items"], result.get("listings"))
