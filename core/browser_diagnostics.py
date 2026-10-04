"""Bounded browser evidence for failed loads, without URLs, cookies or bodies."""

from collections import deque
import re
import time
from urllib.parse import urlsplit


class BrowserDiagnostics:
    def __init__(self, hosts: set[str]):
        self.hosts = hosts
        self.events = deque(maxlen=8)
        self.pending: dict[object, tuple[str, float]] = {}
        self.document_status: int | None = None
        self.access_status: int | None = None

    def _label(self, request) -> str:
        host = urlsplit(request.url).hostname
        kind = request.resource_type
        if host not in self.hosts or kind not in ("document", "script", "xhr", "fetch"):
            return ""
        return f"{host} {kind}"

    def attach(self, page):
        """Install before navigation; deliberately blocked images/CSS are ignored."""
        if getattr(page, "_rtjobs_diagnostics", None) is self:
            return
        page._rtjobs_diagnostics = self
        page.on("request", self._started)
        page.on("requestfinished", self._finished)
        page.on("requestfailed", self._failed)
        page.on("response", self._response)
        page.on("pageerror", self._page_error)

    def reset(self):
        self.events.clear()
        self.pending.clear()
        self.document_status = None
        self.access_status = None

    def _started(self, request):
        label = self._label(request)
        if label and len(self.pending) < 64:
            self.pending[request] = (label, time.monotonic())

    def _finished(self, request):
        self.pending.pop(request, None)

    def _failed(self, request):
        self._finished(request)
        label = self._label(request)
        # Reloads intentionally abort old requests; they are not transport faults.
        code = re.search(r"\bERR_[A-Z0-9_]+\b", request.failure or "")
        if label and code and code[0] not in ("ERR_ABORTED", "ERR_BLOCKED_BY_CLIENT"):
            self.events.append(f"Transport: {label} {code[0]}")

    def _response(self, response):
        request = response.request
        label = self._label(request)
        if not label:
            return
        if request.resource_type == "document":
            self.document_status = response.status
        if response.status >= 400:
            self.events.append(f"HTTP: {label} {response.status}")
            if response.status in (401, 403, 429):
                self.access_status = response.status

    def _page_error(self, error):
        # Error names identify JS failures without copying app/account data.
        name = getattr(error, "name", type(error).__name__)
        if re.fullmatch(r"[A-Za-z]+Error", str(name)):
            self.events.append(f"Page JavaScript: {name}")

    def navigation_error(self, error):
        """Keep the error code even if Chromium closed the page before events."""
        code = re.search(r"\bERR_[A-Z0-9_]+\b", str(error))
        self.events.append("Navigation: " + (code[0] if code else type(error).__name__))

    def summary(self) -> str:
        evidence = list(dict.fromkeys(self.events))[-3:]
        now = time.monotonic()
        pending = sorted(self.pending.values(), key=lambda item: item[1])
        for label, since in pending[:3]:
            if now - since >= 10:
                evidence.append(f"Pending: {label} {int(now - since)}s")
        return "; ".join(evidence) or "No transport/HTTP error was captured; cause remains unconfirmed."
