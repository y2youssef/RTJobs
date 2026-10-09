"""NaukriGulf job board: Akamai-protected, client-rendered, public (no login).

Same Chrome/CDP model as the other boards: we launch it with an HTTP
DevTools endpoint (CHROME_DEBUG_PORT + 3) and the spider connects to it.
The persistent profile keeps Akamai's sensor cookies and trust history.
Jobs come from the search API response the page itself receives (see
boards/naukrigulf/scraper.py and NAUKRIGULF.md).
"""

import logging

from boards.base import JobBoard
from boards.naukrigulf import scraper
from config import NAUKRIGULF_ENABLED, NAUKRIGULF_PROFILE_DIR
from core import timing
from core.scrape_health import ScrapeHealth

logger = logging.getLogger(__name__)


class NaukriGulfBoard(JobBoard):
    name = "naukrigulf"
    title = "NaukriGulf"
    enabled = NAUKRIGULF_ENABLED
    profile_dir = NAUKRIGULF_PROFILE_DIR
    port_offset = 3

    def scrape(self, cdp: str, record) -> int:
        health = ScrapeHealth(self.name)
        with timing.stage(self.name, "scrape_spider",
                          lambda: {"items": len(result["items"]), "status": health.status}):
            result = scraper.scrape(self.selectors, cdp_url=cdp, health=health)
        return self.finish_scrape(record, health, result["items"], result.get("listings"))
