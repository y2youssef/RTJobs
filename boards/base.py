"""Job board abstraction: each site (LinkedIn, Wuzzuf, ...) is a JobBoard."""

import logging
import json
import os
from abc import ABC, abstractmethod

from config import (BOARD_TIME_BUDGET_SECONDS, CHROME_DEBUG_PORT, HEADLESS,
                    KILL_CHROME_ON_START, MARKUP_DIR)
from core.board_budget import BoardTimeout, time_budget
from core.browser import chrome_session, install_cdp_default_context_patch

logger = logging.getLogger(__name__)


def persist_jobs(source: str, items: list[dict], listings: dict[str, str] | None = None) -> int:
    """Save jobs with blocklist filtering and optional enrichment queuing.

    Returns new_count. Blocked jobs are marked seen so they
    are never re-scraped, but never saved/notified. Shared by all boards.
    `listings` (external_id -> current listing time of already-known cards)
    feeds repost detection (core/db.record_listings).
    """
    from core import blocklist, db, timing

    accepted = []
    blocked = []
    blocked_names: list[str] = []
    for job in items or []:
        if blocklist.is_blocked(job.get("source") or source, job.get("company") or ""):
            blocked.append((job["source"], str(job["external_id"])))
            blocked_names.append(job.get("company") or "?")
            continue
        accepted.append(job)
    with timing.stage(source, "persist_jobs", lambda: {"new": new_count, "blocked": len(blocked)}):
        new_count = db.save_jobs(accepted, blocked)
    if blocked_names:
        logger.info(
            f"[{source}] Filtered out {len(blocked_names)} blocked-company"
            f" job(s): {', '.join(sorted(set(blocked_names)))}"
        )
    logger.info(f"[{source}] Saved {new_count} new job(s)")
    if listings:
        reposts = db.record_listings(source, listings)
        if reposts["reposts"]:
            logger.info(f"[{source}] Reposts detected: {reposts['reposts']}; "
                        f"re-sending {len(reposts['requeued'])} (last delivered over the cooldown ago)")

    # Notification happens after Chrome closes (main.py), or in the separate
    # enrichment worker. No network waits inside the browser's lifetime here.
    return new_count


def report_run_checks(source: str, checks: dict[str, tuple[bool, str]]):
    """Run-level health (crash, time budget, selector config) with the usual
    once-per-episode alert dedupe: a board failing every 3 minutes alerts
    once, and the check recovers when a run completes normally."""
    from core.scrape_health import ScrapeHealth

    health = ScrapeHealth(source)
    for name, (good, detail) in checks.items():
        health.check(name, good, detail)
    health.report(require_search=False, subject=f"{source}: board run failing",
                  hint="Repeats are suppressed until a run of this board completes normally.")


def load_board_selectors(site: str) -> dict:
    """Load <MARKUP_DIR>/<site>/selectors.json. Raises FileNotFoundError
    if the config is missing (startup fails loudly instead of scraping blind)."""
    path = os.path.join(MARKUP_DIR, site, "selectors.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class RunRecord:
    """One `runs` row, finished exactly once; later finish() calls are ignored,
    so an outer handler can never overwrite the precise inner status."""

    def __init__(self, source: str):
        from core import db

        self._db = db
        self.id = db.start_run(source)
        self.done = False

    def finish(self, status: str, **kw):
        if not self.done:
            self.done = True
            self._db.finish_run(self.id, status, **kw)


class JobBoard(ABC):
    """A single job board site.

    run() is the shared template: a run row, pre-browser checks, our own
    CDP Chrome on the board's persistent profile, then the board's scrape().
    A scrape error is recorded and alerted and the cycle continues with 0
    jobs; Chrome startup failures and interrupts propagate to main.py.
    """

    name: str = "base"
    title: str = "Base"  # alert wording: "<title> board failed"
    enabled: bool = True
    profile_dir: str = ""
    # CDP port = CHROME_DEBUG_PORT + offset, so boards can run in parallel.
    port_offset: int = 0

    def __init__(self):
        self.selectors = load_board_selectors(self.name)

    def run(self) -> int:
        """Scrape the board and persist jobs; delivery runs after Chrome closes.

        Returns the number of newly scraped jobs (0 is a valid result).
        """
        record = RunRecord(self.name)
        try:
            skip = self.before_browser()
            if skip:
                record.finish(skip)
                return 0
            install_cdp_default_context_patch()
            with chrome_session(self.profile_dir, CHROME_DEBUG_PORT + self.port_offset, headless=HEADLESS,
                                clean_locks=KILL_CHROME_ON_START) as cdp:
                try:
                    with time_budget(BOARD_TIME_BUDGET_SECONDS):
                        new_count = self.scrape(cdp, record)
                    report_run_checks(self.name, {"board_run": (True, ""), "time_budget": (True, "")})
                    return new_count
                except BoardTimeout:
                    detail = (f"{self.title} exceeded its {BOARD_TIME_BUDGET_SECONDS}s time budget and was "
                              "stopped so the other boards' jobs are not held back; jobs from this run "
                              "were not saved and the next run retries.")
                    record.finish("timeout", error=detail)
                    logger.error(f"[{self.name}] {detail}")
                    report_run_checks(self.name, {"time_budget": (False, detail)})
                    return 0
                except SystemExit:
                    record.finish("interrupted", error="terminated by signal")
                    raise
                except Exception as exc:
                    record.finish("error", error=str(exc))
                    logger.error(f"[{self.name}] Run failed: {exc}")
                    report_run_checks(self.name, {"board_run": (False, f"{self.title} board failed: {exc}"[:600])})
                    return 0
        except BaseException as exc:
            # Chrome launch failures and interrupts outside scrape().
            record.finish("interrupted" if isinstance(exc, SystemExit) else "error", error=str(exc))
            raise
        finally:
            # Structural guarantee: a scrape() path that forgets record.finish()
            # can never leave the row "running" (all current paths finish it).
            if not record.done:
                record.finish("error", error=f"{self.name}: scrape() returned without recording a status")

    def before_browser(self) -> str | None:
        """Checks that must run before Chrome starts (never inside a live
        Chrome); return a run status to skip this run."""
        return None

    @abstractmethod
    def scrape(self, cdp: str, record: RunRecord) -> int:
        """Board-specific work against the running Chrome; finish `record`."""

    def finish_scrape(self, record: RunRecord, health, items: list[dict],
                      listings: dict[str, str] | None = None) -> int:
        """Persist the scraped jobs and close the run with the health status."""
        new_count = persist_jobs(self.name, items, listings)
        record.finish(health.status, jobs_found=new_count, error=health.error)
        return new_count
