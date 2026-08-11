"""Wuzzuf job board: Cloudflare-protected SSR search pages, no login."""

from boards.base import JobBoard
from boards.wuzzuf import scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    KILL_CHROME_ON_START,
    WUZZUF_PROFILE_DIR,
)
from core import db, telegram
from core.browser import (
    cdp_url_for,
    install_cdp_default_context_patch,
    launch_cdp_chrome,
    stop_chrome,
)

install_cdp_default_context_patch()


class WuzzufBoard(JobBoard):
    name = "wuzzuf"
    requires_login = False
    enabled = True

    def run(self) -> int:
        run_id = db.start_run(self.name)

        # Same Chrome/CDP model as LinkedIn: we launch it so the session is
        # live-attachable on the debug port, and the spider connects to it.
        # The persistent profile keeps the Cloudflare clearance cookie.
        chrome = launch_cdp_chrome(
            WUZZUF_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
            clean_locks=KILL_CHROME_ON_START,
        )

        try:
            items = scraper.scrape(self.selectors, cdp_url=cdp_url_for(CHROME_DEBUG_PORT))

            new_count = 0
            for job in items:
                db.save_job(job)
                new_count += 1
            print(f"[wuzzuf] Saved {new_count} new job(s)")

            sent = telegram.notify_jobs(db.get_unnotified(self.name))
            print(f"[wuzzuf] Notified {sent} job(s)")

            db.finish_run(run_id, "ok", jobs_found=new_count)
            return new_count

        except Exception as e:
            db.finish_run(run_id, "error", error=str(e))
            telegram.notify_failure("Wuzzuf board failed", str(e))
            print(f"[wuzzuf] Run failed: {e}")
            return 0
        finally:
            stop_chrome(chrome)
