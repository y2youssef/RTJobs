"""Report observed parser failures once per episode, across scheduled runs.

Checks describe missing evidence, not a proven cause: a changed layout, expired
job, incomplete load or network failure can produce the same symptom. Optional
fields (salary, recruiter, etc.) are not required on every posting.
"""

import logging

from core import db, markup, telegram

logger = logging.getLogger(__name__)


class ScrapeHealth:
    def __init__(self, source: str):
        self.source = source
        self.checks: dict[str, dict] = {}
        self.snapshot: str | None = None

    @property
    def status(self) -> str:
        """A completed spider can still have produced incomplete data."""
        return "degraded" if any(not c["good"] for c in self.checks.values()) else "ok"

    @property
    def error(self) -> str:
        return "; ".join(f"{name}: {check['detail']}" for name, check in self.checks.items()
                         if not check["good"])[:1800]

    def check(self, name: str, good: bool, detail: str = "", html: str = ""):
        """A later successful card must not erase an earlier failure this run."""
        if name in self.checks and not self.checks[name]["good"]:
            return
        if not good:
            logger.warning("[%s] Scrape check %s: %s", self.source, name, detail)
            if html and not self.snapshot:
                self.snapshot = markup.save_snapshot(self.source, "parse_health", html)
        self.checks[name] = {"good": bool(good), "detail": detail[:700] if not good else "",
                             "snapshot": self.snapshot if not good else None}

    async def page_failure(self, name: str, detail: str, page):
        """Capture evidence without letting a closed page hide the original error."""
        try:
            html = await page.content()
        except Exception:
            html = ""
        self.check(name, False, detail, html)

    def job_fields(self, jobs: list[dict], html: str = "", description: bool = True):
        """Require identity/title/full text; flag widespread missing companies."""
        if not jobs:
            return  # All-seen pages must not clear untested detail failures.
        fields = ["external_id", "title"] + (["description"] if description else [])
        for field in fields:
            missing = [str(j.get("external_id") or "?") for j in jobs if not j.get(field)]
            self.check("field_" + field, not missing,
                       f"{field} missing on {len(missing)}/{len(jobs)} parsed jobs; "
                       f"sample IDs: {', '.join(missing[:3])}", html)
        # A confidential employer can legitimately be blank; widespread loss
        # across a sample is a useful selector-change signal.
        if len(jobs) >= 3:
            missing = sum(not j.get("company") for j in jobs)
            self.check("field_company", missing * 2 < len(jobs),
                       f"Company missing on {missing}/{len(jobs)} parsed jobs.", html)

    def report(self, require_search: bool = True):
        """One error-channel message for new failures; retry unsuccessful alerts.

        Persist small diagnostics only. Sanitized snapshots strip scripts; they
        are selector evidence, not full embedded JSON or account-state dumps.
        require_search=False reports checks made outside a spider run.
        """
        if require_search and "search_fetch" not in self.checks:
            self.check("search_fetch", False, "The spider completed without parsing a search response.")
        with db.get_db() as conn:
            previous = {r["check_name"]: dict(r) for r in conn.execute(
                "SELECT * FROM scrape_health WHERE source=?", (self.source,))}
        fresh = [name for name, check in self.checks.items() if not check["good"]
                 and not (previous.get(name, {}).get("failing")
                          and previous.get(name, {}).get("last_alert_at"))]
        sent = False
        if fresh:
            details = "\n".join(f"{name}: {self.checks[name]['detail']}" for name in fresh)
            try:
                sent = telegram.notify_failure(
                    f"{self.source}: scraping data check failed",
                    details[:1800], self.snapshot,
                    "Possible layout, loading or access change. Check the snapshot and scraper logs; "
                    "successful page loading does not guarantee complete data.",
                )
            except Exception as exc:
                logger.warning("[%s] Health alert failed: %s", self.source, type(exc).__name__)
        now = db.now_str()
        with db.get_db() as conn:
            for name, check in self.checks.items():
                old = previous.get(name, {})
                failing = not check["good"]
                continuing = failing and old.get("failing")
                first_seen = old.get("first_seen_at") if continuing else now
                last_alert = old.get("last_alert_at") if continuing else None
                if name in fresh and sent:
                    last_alert = now
                conn.execute("INSERT INTO scrape_health VALUES (?,?,?,?,?,?,?,?) "
                             "ON CONFLICT(source,check_name) DO UPDATE SET "
                             "failing=excluded.failing, detail=excluded.detail, snapshot=excluded.snapshot, "
                             "first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at, "
                             "last_alert_at=excluded.last_alert_at",
                             (self.source, name, int(failing), check["detail"], check["snapshot"],
                              first_seen, now, last_alert))
                if check["good"] and old.get("failing"):
                    logger.info("[%s] Scrape check recovered: %s", self.source, name)
