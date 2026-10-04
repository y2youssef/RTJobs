"""Indeed job board: login-gated JSON blob pages (see INDEED.md).

Same Chrome/CDP model as the other boards: we launch Chrome ourselves
(persistent profile keeps the Cloudflare clearance AND the Indeed login
session) with an HTTP DevTools endpoint for live debugging. A sync login
check runs first (LinkedIn pattern); only then does the async spider run.
"""

import logging
from scrapling.fetchers import StealthySession

from boards.base import JobBoard, persist_jobs
from boards.indeed import login, scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    INDEED_ENABLED,
    INDEED_PROFILE_DIR,
    INDEED_SEARCH_URL,
    KILL_CHROME_ON_START,
)
from core import db, telegram
from core.scrape_health import ScrapeHealth
from core.browser import (
    chrome_session,
    install_cdp_default_context_patch,
    patch_no_load_wait,
)

logger = logging.getLogger(__name__)

install_cdp_default_context_patch()


class IndeedBoard(JobBoard):
    name = "indeed"
    requires_login = True
    enabled = INDEED_ENABLED

    def run(self) -> int:
        run_id = db.start_run(self.name)

        try:
            with chrome_session(
                INDEED_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
                clean_locks=KILL_CHROME_ON_START,
            ) as cdp:
                return self._run(cdp, run_id)
        except BaseException as exc:
            status = "interrupted" if isinstance(exc, SystemExit) else "error"
            db.finish_run(run_id, status, error=str(exc))
            raise


    def _run(self, cdp: str, run_id: int) -> int:
        outcome: dict = {"ok": False}
        finished = False

        def _finish(status: str, **kw):
            nonlocal finished
            if not finished:
                finished = True
                db.finish_run(run_id, status, **kw)

        def page_action(page):
            outcome["ok"] = login.ensure_logged_in(page, self.selectors)

        try:
            with StealthySession(
                cdp_url=cdp,
                solve_cloudflare=True,
                timeout=120_000,
                page_setup=patch_no_load_wait,
                page_action=page_action,
            ) as session:
                logger.info("[indeed] Opening search page (login check first)")
                session.fetch(INDEED_SEARCH_URL, wait=5000)

            if not outcome["ok"]:
                logger.info("[indeed] Login check failed — skipping scrape.")
                _finish("login_failed")
                return 0

            health = ScrapeHealth(self.name)
            result = scraper.scrape(self.selectors, cdp_url=cdp, health=health)

            if result["logged_out"]:
                logger.info("[indeed] Session died mid-scrape — aborting.")
                _finish("session_expired")
                login.mark_logged_out()
                return 0

            if result["blocked"]:
                logger.info("[indeed] Block page mid-scrape — aborting.")
                _finish("blocked_page")
                login.mark_blocked_page()
                return 0

            new_count = persist_jobs(self.name, result["items"])

            login.clear_episode_flags()
            _finish(health.status, jobs_found=new_count, error=health.error)
            return new_count

        except BaseException as e:
            if isinstance(e, SystemExit):
                _finish("interrupted", error="terminated by signal")
                raise
            _finish("error", error=str(e))
            telegram.notify_failure(
                "Indeed board failed",
                str(e),
            )
            logger.error(f"[indeed] Run failed: {e}")
            return 0
