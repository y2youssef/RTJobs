"""LinkedIn job board: login + scrape orchestration."""

from scrapling.fetchers import StealthySession

from boards.base import JobBoard
from boards.linkedin import login, scraper
from config import (
    CHROME_ARGS,
    HEADLESS,
    LINKEDIN_ENABLED,
    LINKEDIN_LOGIN_URL,
    LINKEDIN_PROFILE_DIR,
)
from core import db, login_state, telegram
from core.browser import patch_no_load_wait


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
            print(f"[linkedin] Skipping run — blocked for {remaining}s.")
            db.finish_run(run_id, "blocked")
            return 0

        # Maxed retries + cooldown expired -> fresh login. The profile must
        # be wiped BEFORE the browser starts (never from under a live Chrome).
        if login_state.should_wipe_profile():
            print("[linkedin] Cooldown over, retry count maxed — wiping profile.")
            login.wipe_profile()
            login_state.reset_retries()

        outcome: dict = {"ok": False}

        def page_action(page):
            outcome["ok"] = login.ensure_logged_in(page, self.selectors)

        try:
            with StealthySession(
                headless=HEADLESS,
                real_chrome=True,
                user_data_dir=LINKEDIN_PROFILE_DIR,
                extra_flags=CHROME_ARGS,
                disable_resources=True,
                timeout=30_000,
                page_setup=patch_no_load_wait,
                page_action=page_action,
            ) as session:
                print("[linkedin] Opening login page (redirects to feed if active)")
                session.fetch(LINKEDIN_LOGIN_URL, wait=5000)

            if not outcome["ok"]:
                print("[linkedin] Login check failed — skipping scrape.")
                db.finish_run(run_id, "login_failed")
                return 0

            result = scraper.scrape(self.selectors)

            if result["login_redirect"]:
                print("[linkedin] Session died mid-scrape — aborting.")
                db.finish_run(run_id, "session_expired")
                telegram.notify_failure(
                    "LinkedIn session expired mid-scrape",
                    "The browser was redirected to login while scraping."
                    " The next run will re-login.",
                    hint="Check http://localhost:9222 if it persists",
                )
                return 0

            new_count = 0
            for job in result["items"]:
                db.save_job(job)
                new_count += 1
            print(f"[linkedin] Saved {new_count} new job(s)")

            pending = db.get_unnotified(self.name)
            sent = telegram.notify_jobs(pending)
            print(f"[linkedin] Notified {sent} job(s)")

            db.finish_run(run_id, "ok", jobs_found=new_count)
            return new_count

        except SystemExit:
            raise
        except Exception as e:
            db.finish_run(run_id, "error", error=str(e))
            telegram.notify_failure(
                "LinkedIn board failed",
                str(e),
            )
            print(f"[linkedin] Run failed: {e}")
            return 0
