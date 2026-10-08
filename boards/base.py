"""Job board abstraction: each site (LinkedIn, Wuzzuf, ...) is a JobBoard."""

import logging
import json
import os
from abc import ABC, abstractmethod

from config import MARKUP_DIR

logger = logging.getLogger(__name__)


def persist_jobs(source: str, items: list[dict]) -> int:
    """Save jobs with blocklist filtering and optional enrichment queuing.

    Returns new_count. Blocked jobs are marked seen so they
    are never re-scraped, but never saved/notified. Shared by all boards.
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

    # Notification happens after Chrome closes (main.py), or in the separate
    # enrichment worker. No network waits inside the browser's lifetime here.
    return new_count


def load_board_selectors(site: str) -> dict:
    """Load <MARKUP_DIR>/<site>/selectors.json. Raises FileNotFoundError
    if the config is missing (startup fails loudly instead of scraping blind)."""
    path = os.path.join(MARKUP_DIR, site, "selectors.json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class JobBoard(ABC):
    """A single job board site."""

    name: str = "base"
    requires_login: bool = False
    enabled: bool = True

    def __init__(self):
        self.selectors = load_board_selectors(self.name)

    @property
    def markup_site(self) -> str:
        return self.name

    @abstractmethod
    def run(self) -> int:
        """Scrape the board and persist jobs; delivery runs after Chrome closes.

        Returns the number of newly scraped jobs (0 is a valid result).
        """
