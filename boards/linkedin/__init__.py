"""LinkedIn job board: login + scrape orchestration.

Chrome lifecycle: we launch ONE real Chrome with an HTTP DevTools endpoint
(CDP) so the session can be attached live from the host (localhost:9222) for
debugging and manual 2FA/checkpoint solves. The login session and the scrape
spider both connect to that same Chrome over cdp_url and reuse its default
(persistent profile) context, so the login cookies carry into the scrape.
"""

import logging
from scrapling.fetchers import StealthySession

from boards.base import JobBoard, persist_and_notify
from boards.linkedin import login, scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    KILL_CHROME_ON_START,
    LINKEDIN_ENABLED,
    LINKEDIN_LOGIN_URL,
    LINKEDIN_PROFILE_DIR,
)
from core import db, login_state, telegram
from core.browser import (
    chrome_session,
    install_cdp_default_context_patch,
    patch_no_load_wait,
)

logger = logging.getLogger(__name__)

install_cdp_default_context_patch()


class LinkedInBoard(JobBoard):
    name = "linkedin"
    requires_login = True
    enabled = LINKEDIN_ENABLED

    def run(self) -> int:
        run_id = db.start_run(self.name)

        # Cooldown active — don't even start the browser. (Also re-checked
        # inside page_action as a safety net.)
        if login_state.is_blocked():
            remaining = login_state.remaining_seconds()
            logger.info(f"[linkedin] Skipping run — blocked for {remaining}s.")
            db.finish_run(run_id, "blocked")
            return 0

        # Maxed retries + cooldown expired -> fresh login. The profile must
        # be wiped BEFORE the browser starts (never from under a live Chrome).
        if login_state.should_wipe_profile():
            logger.info("[linkedin] Cooldown over, retry count maxed — wiping profile.")
            login.wipe_profile()
            login_state.reset_retries()

        # Clean up zombie Chrome from crashed runs — must happen BEFORE we
        # start our own browser (inside a page_action it would kill itself).
        login.kill_zombie_chrome()

        with chrome_session(
            LINKEDIN_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
            clean_locks=KILL_CHROME_ON_START,
        ) as cdp:
            return self._run(cdp, run_id)

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
                disable_resources=True,
                timeout=30_000,
                page_setup=patch_no_load_wait,
                page_action=page_action,
            ) as session:
                logger.info("[linkedin] Opening login page (redirects to feed if active)")
                session.fetch(LINKEDIN_LOGIN_URL, wait=5000)

            if not outcome["ok"]:
                logger.info("[linkedin] Login check failed — skipping scrape.")
                _finish("login_failed")
                return 0

            result = scraper.scrape(self.selectors, cdp_url=cdp)

            if result["login_redirect"]:
                logger.info("[linkedin] Session died mid-scrape — aborting.")
                _finish("session_expired")
                telegram.notify_failure(
                    "LinkedIn session expired mid-scrape",
                    "The browser was redirected to login while scraping."
                    " The next run will re-login.",
                    hint="Check http://localhost:9222 if it persists",
                )
                return 0

            new_count, _sent = persist_and_notify(self.name, result["items"])

            _finish("ok", jobs_found=new_count)
            return new_count

        except BaseException as e:
            if isinstance(e, SystemExit):
                _finish("interrupted", error="terminated by signal")
                raise
            _finish("error", error=str(e))
            telegram.notify_failure(
                "LinkedIn board failed",
                str(e),
            )
            logger.error(f"[linkedin] Run failed: {e}")
            return 0
