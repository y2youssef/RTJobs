"""Indeed job board: Cloudflare-gated JSON blob pages, no login, no pagination.

Single sort=date search page polled every run (see INDEED.md); job keys get
a follow-up /viewjob?jk= fetch for the full description.
"""

import logging
from boards.base import JobBoard, persist_and_notify
from boards.indeed import scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    INDEED_ENABLED,
    INDEED_PROFILE_DIR,
    KILL_CHROME_ON_START,
)
from core import db, telegram
from core.browser import (
    chrome_session,
    install_cdp_default_context_patch,
)

logger = logging.getLogger(__name__)

install_cdp_default_context_patch()


class IndeedBoard(JobBoard):
    name = "indeed"
    requires_login = False
    enabled = INDEED_ENABLED

    def run(self) -> int:
        run_id = db.start_run(self.name)
        finished = False

        def _finish(status: str, **kw):
            nonlocal finished
            if not finished:
                finished = True
                db.finish_run(run_id, status, **kw)

        # Same Chrome/CDP model as the other boards: we launch it so the
        # session is live-attachable on the debug port; the spider connects
        # to it. The persistent profile keeps the Cloudflare clearance
        # cookie (a plain-curl session gets a 403 Security Check).
        with chrome_session(
            INDEED_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
            clean_locks=KILL_CHROME_ON_START,
        ) as cdp:
            try:
                items = scraper.scrape(self.selectors, cdp_url=cdp)

                new_count, _sent = persist_and_notify(self.name, items)

                _finish("ok", jobs_found=new_count)
                return new_count

            except BaseException as e:
                if isinstance(e, SystemExit):
                    _finish("interrupted", error="terminated by signal")
                    raise
                _finish("error", error=str(e))
                telegram.notify_failure("Indeed board failed", str(e))
                logger.error(f"[indeed] Run failed: {e}")
                return 0
