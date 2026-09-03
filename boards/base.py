"""Job board abstraction: each site (LinkedIn, Wuzzuf, ...) is a JobBoard."""

import logging
import json
import os
from abc import ABC, abstractmethod

from config import MARKUP_DIR

logger = logging.getLogger(__name__)


def persist_and_notify(source: str, items: list[dict]) -> tuple[int, int]:
    """Save jobs (with blocklist filtering) and send Telegram notifications.

    Returns (new_count, sent_count). Blocked jobs are `mark_seen`'d so they
    are never re-scraped, but never saved/notified. Shared by all boards.
    """
    from core import blocklist, db, telegram

    new_count = 0
    blocked_names: list[str] = []
    for job in items or []:
        if blocklist.is_blocked(job.get("source") or source, job.get("company") or ""):
            db.mark_seen(job["source"], job["external_id"])
            blocked_names.append(job.get("company") or "?")
            continue
        db.save_job(job)
        new_count += 1
    if blocked_names:
        logger.info(
            f"[{source}] Filtered out {len(blocked_names)} blocked-company"
            f" job(s): {', '.join(sorted(set(blocked_names)))}"
        )
    logger.info(f"[{source}] Saved {new_count} new job(s)")

    sent = telegram.notify_jobs(db.get_unnotified(source))
    logger.info(f"[{source}] Notified {sent} job(s)")
    return new_count, sent


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
        """Scrape the board, persist jobs, and notify.

        Returns the number of newly scraped jobs (0 is a valid result).
        """
