"""Indeed job board: Cloudflare-gated JSON blob pages, no login, no pagination.

Single sort=date search page polled every run (see INDEED.md); job keys get
a follow-up /viewjob?jk= fetch for the full description.
"""

from boards.base import JobBoard
from boards.indeed import scraper
from config import (
    CHROME_DEBUG_PORT,
    HEADLESS,
    INDEED_ENABLED,
    INDEED_PROFILE_DIR,
    KILL_CHROME_ON_START,
)
from core import blocklist, db, telegram
from core.browser import (
    cdp_url_for,
    install_cdp_default_context_patch,
    launch_cdp_chrome,
    stop_chrome,
)

install_cdp_default_context_patch()


class IndeedBoard(JobBoard):
    name = "indeed"
    requires_login = False
    enabled = INDEED_ENABLED

    def run(self) -> int:
        run_id = db.start_run(self.name)

        # Same Chrome/CDP model as the other boards: we launch it so the
        # session is live-attachable on the debug port; the spider connects
        # to it. The persistent profile keeps the Cloudflare clearance
        # cookie (a plain-curl session gets a 403 Security Check).
        chrome = launch_cdp_chrome(
            INDEED_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
            clean_locks=KILL_CHROME_ON_START,
        )

        try:
            items = scraper.scrape(self.selectors, cdp_url=cdp_url_for(CHROME_DEBUG_PORT))

            new_count = 0
            blocked_names: list[str] = []
            for job in items:
                if blocklist.is_blocked(job["source"], job.get("company") or ""):
                    db.mark_seen(job["source"], job["external_id"])
                    blocked_names.append(job.get("company") or "?")
                    continue
                db.save_job(job)
                new_count += 1
            if blocked_names:
                print(
                    f"[indeed] Filtered out {len(blocked_names)} blocked-company"
                    f" job(s): {', '.join(sorted(set(blocked_names)))}"
                )
            print(f"[indeed] Saved {new_count} new job(s)")

            sent = telegram.notify_jobs(db.get_unnotified(self.name))
            print(f"[indeed] Notified {sent} job(s)")

            db.finish_run(run_id, "ok", jobs_found=new_count)
            return new_count

        except Exception as e:
            db.finish_run(run_id, "error", error=str(e))
            telegram.notify_failure("Indeed board failed", str(e))
            print(f"[indeed] Run failed: {e}")
            return 0
        finally:
            stop_chrome(chrome)
