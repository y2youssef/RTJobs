"""Job board abstraction: each site (LinkedIn, Wuzzuf, ...) is a JobBoard."""

import json
import os
from abc import ABC, abstractmethod

from config import MARKUP_DIR


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
