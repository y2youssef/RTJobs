"""Indeed job board: login-gated JSON blob pages (see INDEED.md).

Same Chrome/CDP model as the other boards: we launch Chrome ourselves
(persistent profile keeps the Cloudflare clearance AND the Indeed login
session) with an HTTP DevTools endpoint for live debugging. The async
spider runs first; the sync login check runs only when its search page
lands logged out, then the spider runs again (LinkedIn pattern).
"""

import logging
from scrapling.fetchers import StealthySession

from boards.base import JobBoard
from boards.indeed import login, scraper
from config import INDEED_ENABLED, INDEED_PROFILE_DIR, INDEED_SEARCH_URL
from core import timing
from core.browser import patch_no_load_wait
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)


class IndeedBoard(JobBoard):
    name = "indeed"
    title = "Indeed"
    enabled = INDEED_ENABLED
    profile_dir = INDEED_PROFILE_DIR
    port_offset = 2

    def scrape(self, cdp: str, record) -> int:
        # Spider first: its search scan already detects a logged-out landing.
        # The login check used to load the full search page (through the
        # Cloudflare solver) only to judge the URL, then the spider loaded it
        # again: ~10s per run. It now runs only when the spider lands logged out.
        health, result = self._spider(cdp)
        if result["logged_out"]:
            logger.info("[indeed] Logged out on the search page — running the login check.")
            if not self._login_check(cdp):
                logger.info("[indeed] Login check failed — skipping scrape.")
                record.finish("login_failed")
                return 0
            if self.waited_for_person():  # the emailed code: the cycle went on without us
                return self.finish_after_person_wait(record)
            health, result = self._spider(cdp)

        if result["logged_out"]:
            logger.info("[indeed] Session died mid-scrape — aborting.")
            record.finish("session_expired")
            login.mark_logged_out()
            return 0

        if result["blocked"]:
            logger.info("[indeed] Block page mid-scrape — aborting.")
            record.finish("blocked_page")
            login.mark_blocked_page()
            return 0

        login.clear_episode_flags()
        return self.finish_scrape(record, health, result["items"], result.get("listings"))

    def _spider(self, cdp: str):
        health = ScrapeHealth(self.name)
        with timing.stage(self.name, "scrape_spider",
                          lambda: {"items": len(result["items"]), "status": health.status}):
            result = scraper.scrape(self.selectors, cdp_url=cdp, health=health, on_job=self.save_now)
        return health, result

    def _login_check(self, cdp: str) -> bool:
        """Load the search page and let login.ensure_logged_in sign in (email
        code via Telegram) when it lands on Indeed's auth page."""
        outcome: dict = {"ok": False}

        def page_action(page):
            outcome["ok"] = login.ensure_logged_in(page, self.selectors)

        with StealthySession(
            cdp_url=cdp,
            solve_cloudflare=True,
            timeout=120_000,
            page_setup=patch_no_load_wait,
            page_action=page_action,
        ) as session:
            logger.info("[indeed] Opening search page for the login check")
            with timing.stage("indeed", "login_check"):
                # wait=0: scrapling's wait runs AFTER the login page_action
                # has decided; it only added 5s idle per run.
                session.fetch(INDEED_SEARCH_URL, wait=0)
        return outcome["ok"]
