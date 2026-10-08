"""Wuzzuf job board: Cloudflare-protected SSR search pages, no login.

Same Chrome/CDP model as LinkedIn: we launch it so the session is
live-attachable on the debug port, and the spider connects to it. The
persistent profile keeps the Cloudflare clearance cookie.
"""

import logging

from boards.base import JobBoard
from boards.wuzzuf import scraper
from config import WUZZUF_ENABLED, WUZZUF_PROFILE_DIR
from core import timing
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)


class WuzzufBoard(JobBoard):
    name = "wuzzuf"
    title = "Wuzzuf"
    enabled = WUZZUF_ENABLED
    profile_dir = WUZZUF_PROFILE_DIR

    def scrape(self, cdp: str, record) -> int:
        health = ScrapeHealth(self.name)
        with timing.stage(self.name, "scrape_spider",
                          lambda: {"items": len(items), "status": health.status}):
            items = scraper.scrape(self.selectors, cdp_url=cdp, health=health)
        return self.finish_scrape(record, health, items)
