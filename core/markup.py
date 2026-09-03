"""Save HTML snapshots of scraped pages for offline selector debugging.

Snapshots land in <MARKUP_DIR>/<site>/snapshots/<kind>/ and are pruned to
keep the folder bounded. The same folder also holds selectors.json per site.

Snapshots are sanitized on save: scripts, styles, and other non-structural
content are stripped so the files stay small and readable as selector
references.
"""

import logging
import os
import re
from datetime import datetime, timezone
from html.parser import HTMLParser

from config import MARKUP_DIR, MAX_SNAPSHOTS_PER_KIND

logger = logging.getLogger(__name__)

_SAFE_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
)

# Elements that carry no scraping value and bloat snapshots.
_SKIP_TAGS = {
    "script", "style", "noscript", "iframe", "svg", "template", "link", "meta",
}

# Void elements never receive an end tag — skip the single tag without
# touching the skip depth, otherwise one <meta> would swallow the document.
_VOID_TAGS = {
    "meta", "link", "br", "img", "input", "hr", "base", "area", "source",
    "track", "wbr", "embed", "col",
}


class _Sanitizer(HTMLParser):
    """Re-emit the document without script/style/etc. blocks and comments."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self._skip_depth = 0
        self._strip_classes = False

    def _render(self, tag: str, attrs, self_closing: bool) -> str:
        if self._strip_classes:
            attrs = [(n, v) for n, v in attrs if n != "class"]
        parts = [f"<{tag}"]
        for name, value in attrs:
            if value is None:
                parts.append(f" {name}")
            else:
                parts.append(f' {name}="{value.replace(chr(34), "&quot;")}"')
        parts.append("/>" if self_closing else ">")
        return "".join(parts)

    def _emit(self, chunk: str):
        if self._skip_depth == 0:
            self.out.append(chunk)

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS:
                self._skip_depth += 1
            return
        self._emit(self._render(tag, attrs, self_closing=False))

    def handle_startendtag(self, tag, attrs):
        if tag not in _SKIP_TAGS:
            self._emit(self._render(tag, attrs, self_closing=True))

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS:
            if tag not in _VOID_TAGS and self._skip_depth > 0:
                self._skip_depth -= 1
            return
        self._emit(f"</{tag}>")

    def handle_data(self, data):
        if data.strip():
            self._emit(data)

    def handle_comment(self, data):
        pass


def sanitize_html(html: str, strip_classes: bool = False) -> str:
    """Strip scripts/styles/meta/comments and collapse whitespace.

    strip_classes additionally removes `class` attributes — use it only for
    reference files where no selector relies on class names (LinkedIn's
    classes are hashed and change on every page load anyway).
    """
    parser = _Sanitizer()
    parser._strip_classes = strip_classes
    parser.feed(html or "")
    parser.close()
    cleaned = "".join(parser.out)
    cleaned = re.sub(r">\s+<", "><", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    return cleaned


def _safe_name(name: str) -> str:
    return "".join(c if c in _SAFE_CHARS else "_" for c in name)[:60]


def _snapshot_dir(site: str, kind: str) -> str:
    path = os.path.join(MARKUP_DIR, site, "snapshots", kind)
    os.makedirs(path, exist_ok=True)
    return path


def save_snapshot(site: str, kind: str, html: str) -> str | None:
    """Save sanitized HTML and prune old snapshots of the same kind.

    Returns the snapshot path relative to MARKUP_DIR (for messages),
    or None on failure/empty input.
    """
    if not html:
        return None
    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        filename = f"{_safe_name(kind)}_{stamp}.html"
        path = os.path.join(_snapshot_dir(site, kind), filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(sanitize_html(html))
        _prune(site, kind)
        return os.path.relpath(path, MARKUP_DIR)
    except OSError as e:
        logger.warning(f"[markup] Failed to save snapshot: {e}")
        return None


def _prune(site: str, kind: str) -> None:
    directory = _snapshot_dir(site, kind)
    try:
        files = sorted(
            (f for f in os.listdir(directory) if f.endswith(".html")),
            reverse=True,
        )
        for old in files[MAX_SNAPSHOTS_PER_KIND:]:
            os.remove(os.path.join(directory, old))
    except OSError:
        pass
