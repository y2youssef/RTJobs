"""Indeed job board: login-gated JSON blob pages (see INDEED.md).

Same Chrome/CDP model as the other boards: we launch Chrome ourselves
(persistent profile keeps the Cloudflare clearance AND the Indeed login
session) with an HTTP DevTools endpoint for live debugging. A sync login
check runs first (LinkedIn pattern); only then does the async spider run.
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

    def scrape(self, cdp: str, record) -> int:
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
            logger.info("[indeed] Opening search page (login check first)")
            with timing.stage("indeed", "login_check"):
                # wait=0: scrapling's wait runs AFTER the login page_action
                # has decided; it only added 5s idle per run.
                session.fetch(INDEED_SEARCH_URL, wait=0)

        if not outcome["ok"]:
            logger.info("[indeed] Login check failed — skipping scrape.")
            record.finish("login_failed")
            return 0

        health = ScrapeHealth(self.name)
        with timing.stage(self.name, "scrape_spider",
                          lambda: {"items": len(result["items"]), "status": health.status}):
            result = scraper.scrape(self.selectors, cdp_url=cdp, health=health)

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
        return self.finish_scrape(record, health, result["items"])
