"""Company blocklist: jobs from blocked companies are dropped before
persistence/notification (and marked seen so they're never re-scraped).

Config: <MARKUP_DIR>/blocked_companies.json — kept under markup/ because
that folder is bind-mounted into the container, so the list can be edited
live without rebuilding the image.

Format:
    {
      "*":        ["applies to every source"],
      "linkedin": ["company name", "another"],
      "wuzzuf":   ["..."]
    }
Matching is case-insensitive substring on the normalized (whitespace-
collapsed) company name, so "alignerr" also blocks "Alignerr Inc.".
Missing/invalid file = nothing is blocked (fail open, but loudly logged).
"""

import logging
import json
import os
import re

from config import MARKUP_DIR

logger = logging.getLogger(__name__)

_PATH = os.path.join(MARKUP_DIR, "blocked_companies.json")
_cache: dict | None = None


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip().lower()


def _load() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    try:
        with open(_PATH, encoding="utf-8") as f:
            raw = json.load(f)
        _cache = {
            _normalize(source): [_normalize(c) for c in companies if c]
            for source, companies in raw.items()
            if isinstance(companies, list)
        }
    except FileNotFoundError:
        logger.warning(f"[blocklist] {_PATH} not found — no companies blocked.")
        _cache = {}
    except (ValueError, TypeError, AttributeError) as e:
        logger.warning(f"[blocklist] Could not parse {_PATH}: {e} — nothing blocked.")
        _cache = {}
    return _cache


def is_blocked(source: str, company: str) -> bool:
    """True when the job's company matches an entry for its source or '*'."""
    name = _normalize(company)
    if not name:
        return False
    entries = _load()
    for key in (source, "*"):
        for blocked in entries.get(key, ()):
            if blocked and blocked in name:
                return True
    return False
