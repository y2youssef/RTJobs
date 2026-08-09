"""Wuzzuf job board: Cloudflare-protected SSR search pages, no login."""

from boards.base import JobBoard
from boards.wuzzuf import scraper
from core import db, telegram


class WuzzufBoard(JobBoard):
    name = "wuzzuf"
    requires_login = False
    enabled = True

    def run(self) -> int:
        run_id = db.start_run(self.name)

        try:
            items = scraper.scrape(self.selectors)

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
