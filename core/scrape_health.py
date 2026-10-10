"""Report observed parser failures once per episode, across scheduled runs.

Checks describe missing evidence, not a proven cause: a changed layout, expired
job, incomplete load or network failure can produce the same symptom. Optional
fields (salary, recruiter, etc.) are not required on every posting.
"""

import json
import logging

from core import clock, db, markup, telegram

logger = logging.getLogger(__name__)

# A failure alerts once it repeats. A single failed observation is usually a
# network drop or a slow load that the next run (3 minutes later) recovers on
# its own: on Oct 9 three such blips alerted for nothing, and one alert was
# lost because Telegram was unreachable in the same outage. Twice in a row, or
# FLAP_FAILURES times within FLAP_WINDOW_SECONDS (a check that keeps flapping),
# is an episode for a person.
CONFIRM_STREAK = 2
FLAP_FAILURES = 3
FLAP_WINDOW_SECONDS = 3600


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

    def report(self, require_search: bool = True, subject: str | None = None, hint: str | None = None):
        """One error-channel message per confirmed episode; nothing goes unsaid.

        An episode alerts once it is confirmed (CONFIRM_STREAK failures in a
        row, or FLAP_FAILURES within FLAP_WINDOW_SECONDS); a failed delivery is
        retried at every later failing observation. If a confirmed episode
        recovers before any alert got through, one "failed and recovered"
        note is sent instead (retried on later reports until delivered), so
        an outage that also took Telegram down is still reported.

        Persist small diagnostics only. Sanitized snapshots strip scripts; they
        are selector evidence, not full embedded JSON or account-state dumps.
        require_search=False reports checks made outside a spider run.
        """
        if require_search and "search_fetch" not in self.checks:
            self.check("search_fetch", False, "The spider completed without parsing a search response.")
        with db.get_db() as conn:
            previous = {r["check_name"]: dict(r) for r in conn.execute(
                "SELECT * FROM scrape_health WHERE source=?", (self.source,))}
        now = db.now_str()
        rows, due = {}, []
        for name, check in self.checks.items():
            old = previous.get(name, {})
            was_failing = bool(old.get("failing"))
            try:
                recent = json.loads(old.get("recent_failures") or "[]")
            except (TypeError, ValueError):
                recent = []
            recent = [t for t in recent if (clock.age_seconds(t) or 0) <= FLAP_WINDOW_SECONDS]
            note = old.get("pending_note")
            if not check["good"]:
                recent.append(now)
                streak = (old.get("fail_streak") or 0) + 1 if was_failing else 1
                alerted = old.get("last_alert_at") if was_failing else None
                first_seen = old.get("first_seen_at") if was_failing else now
                if not alerted and (streak >= CONFIRM_STREAK or len(recent) >= FLAP_FAILURES):
                    due.append(name)
            else:
                streak, alerted, first_seen = 0, None, now
                old_streak = old.get("fail_streak") or 0
                if was_failing and not old.get("last_alert_at") and (
                        old_streak >= CONFIRM_STREAK or len(recent) >= FLAP_FAILURES):
                    note = (f"{name}: failing from {clock.to_local(old.get('first_seen_at'))} to "
                            f"{clock.to_local(old.get('last_seen_at'))} ({old_streak} run(s) in a row), "
                            f"recovered {clock.to_local(now)}. Last detail: {(old.get('detail') or '')[:300]}")
            rows[name] = {"failing": int(not check["good"]), "detail": check["detail"], "snapshot": check["snapshot"],
                          "first_seen_at": first_seen, "last_seen_at": now, "last_alert_at": alerted,
                          "fail_streak": streak, "recent_failures": json.dumps(recent), "pending_note": note}

        if due:
            details = "\n".join(f"{name}: {self.checks[name]['detail']}" for name in due)
            if self._send(subject or f"{self.source}: scraping data check failed", details[:1800], self.snapshot,
                          hint or ("Possible layout, loading or access change. Check the snapshot and scraper logs; "
                                   "successful page loading does not guarantee complete data.")):
                for name in due:
                    rows[name]["last_alert_at"] = now
        # Notes: this run's recoveries plus older undelivered ones.
        notes = {name: row["pending_note"] for name, row in rows.items() if row["pending_note"]}
        notes.update({name: old["pending_note"] for name, old in previous.items()
                      if old.get("pending_note") and name not in rows})
        notes_sent = bool(notes) and self._send(
            f"{self.source}: failed and recovered (no alert got through)", "\n".join(notes.values())[:1800], None,
            "For the record: the failure ended before an alert could be delivered. No action needed unless it repeats.")

        with db.get_db() as conn:
            for name, row in rows.items():
                if notes_sent:
                    row["pending_note"] = None
                conn.execute(
                    "INSERT INTO scrape_health (source, check_name, failing, detail, snapshot, first_seen_at,"
                    " last_seen_at, last_alert_at, fail_streak, recent_failures, pending_note)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(source, check_name) DO UPDATE SET"
                    " failing=excluded.failing, detail=excluded.detail, snapshot=excluded.snapshot,"
                    " first_seen_at=excluded.first_seen_at, last_seen_at=excluded.last_seen_at,"
                    " last_alert_at=excluded.last_alert_at, fail_streak=excluded.fail_streak,"
                    " recent_failures=excluded.recent_failures, pending_note=excluded.pending_note",
                    (self.source, name, row["failing"], row["detail"], row["snapshot"], row["first_seen_at"],
                     row["last_seen_at"], row["last_alert_at"], row["fail_streak"], row["recent_failures"],
                     row["pending_note"]))
            if notes_sent:
                for name in notes:
                    if name not in rows:
                        conn.execute("UPDATE scrape_health SET pending_note=NULL WHERE source=? AND check_name=?",
                                     (self.source, name))
        for name, check in self.checks.items():
            if check["good"] and previous.get(name, {}).get("failing"):
                logger.info("[%s] Scrape check recovered: %s", self.source, name)

    def _send(self, subject: str, detail: str, snapshot: str | None, hint: str) -> bool:
        try:
            return bool(telegram.notify_failure(subject, detail, snapshot, hint=hint))
        except Exception as exc:
            logger.warning("[%s] Health alert failed: %s", self.source, type(exc).__name__)
            return False
