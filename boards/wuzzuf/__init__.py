"""Wuzzuf job board: Cloudflare-protected SSR search pages, no login."""

import logging
from boards.base import JobBoard, persist_and_notify
from boards.wuzzuf import scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    KILL_CHROME_ON_START,
    WUZZUF_ENABLED,
    WUZZUF_PROFILE_DIR,
)
from core import db, telegram
from core.browser import (
    chrome_session,
    install_cdp_default_context_patch,
)

logger = logging.getLogger(__name__)

install_cdp_default_context_patch()


class WuzzufBoard(JobBoard):
    name = "wuzzuf"
    requires_login = False
    enabled = WUZZUF_ENABLED

    def run(self) -> int:
        run_id = db.start_run(self.name)
        finished = False

        def _finish(status: str, **kw):
            nonlocal finished
            if not finished:
                finished = True
                db.finish_run(run_id, status, **kw)

        # Same Chrome/CDP model as LinkedIn: we launch it so the session is
        # live-attachable on the debug port, and the spider connects to it.
        # The persistent profile keeps the Cloudflare clearance cookie.
        with chrome_session(
            WUZZUF_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
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
                telegram.notify_failure("Wuzzuf board failed", str(e))
                logger.error(f"[wuzzuf] Run failed: {e}")
                return 0
