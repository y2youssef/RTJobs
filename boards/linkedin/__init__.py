"""LinkedIn job board: login + scrape orchestration.

Chrome lifecycle: we launch ONE real Chrome with an HTTP DevTools endpoint
(CDP) so the session can be attached live from the host (localhost:9222) for
debugging and manual 2FA/checkpoint solves. The login session and the scrape
spider both connect to that same Chrome over cdp_url and reuse its default
(persistent profile) context, so the login cookies carry into the scrape.
"""

from scrapling.fetchers import StealthySession

from boards.base import JobBoard
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
    cdp_url_for,
    install_cdp_default_context_patch,
    launch_cdp_chrome,
    patch_no_load_wait,
    stop_chrome,
)

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
            print(f"[linkedin] Skipping run — blocked for {remaining}s.")
            db.finish_run(run_id, "blocked")
            return 0

        # Maxed retries + cooldown expired -> fresh login. The profile must
        # be wiped BEFORE the browser starts (never from under a live Chrome).
        if login_state.should_wipe_profile():
            print("[linkedin] Cooldown over, retry count maxed — wiping profile.")
            login.wipe_profile()
            login_state.reset_retries()

        # Clean up zombie Chrome from crashed runs — must happen BEFORE we
        # start our own browser (inside a page_action it would kill itself).
        login.kill_zombie_chrome()

        cdp = cdp_url_for(CHROME_DEBUG_PORT)
        chrome = launch_cdp_chrome(
            LINKEDIN_PROFILE_DIR, CHROME_DEBUG_PORT, headless=HEADLESS,
            clean_locks=KILL_CHROME_ON_START,
        )

        try:
            return self._run(chrome, cdp, run_id)
        finally:
            stop_chrome(chrome)

    def _run(self, chrome, cdp: str, run_id: int) -> int:
        outcome: dict = {"ok": False}

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
                print("[linkedin] Opening login page (redirects to feed if active)")
                session.fetch(LINKEDIN_LOGIN_URL, wait=5000)

            if not outcome["ok"]:
                print("[linkedin] Login check failed — skipping scrape.")
                db.finish_run(run_id, "login_failed")
                return 0

            result = scraper.scrape(self.selectors, cdp_url=cdp)

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
