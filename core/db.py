"""SQLite persistence: jobs, seen_ids, runs, and login state."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import (DB_PATH, ENRICHMENT_ENABLED, ENRICHMENT_SCHEMA_VERSION,
                    NOTIFY_BATCH_SIZE, NOTIFY_PER_CHANNEL_LIMIT)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    title       TEXT,
    company     TEXT,
    posted_at   TEXT,
    description TEXT,
    link        TEXT,
    extra       TEXT,
    scraped_at  TEXT,
    notified    INTEGER DEFAULT 0,
    UNIQUE(source, external_id)
);

-- Never pruned: keeps us from re-notifying jobs that fell off the buffer
CREATE TABLE IF NOT EXISTS seen_ids (
    source      TEXT NOT NULL,
    external_id TEXT NOT NULL,
    PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source      TEXT NOT NULL,
    status      TEXT NOT NULL,
    jobs_found  INTEGER DEFAULT 0,
    error       TEXT,
    started_at  TEXT,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS login_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS job_enrichments (
    job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
    country TEXT NOT NULL DEFAULT 'EG',
    state TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT '',
    job_family TEXT,
    specialization TEXT,
    employer_sector TEXT,
    routing_confidence TEXT,
    needs_review INTEGER,
    schema_version TEXT NOT NULL DEFAULT '',
    result_json TEXT,
    usage_json TEXT,
    input_hash TEXT,
    model TEXT,
    version TEXT,
    error TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_enrichment_pending
    ON job_enrichments(state, next_attempt_at, job_id);
CREATE TABLE IF NOT EXISTS enrichment_cache (
    input_hash TEXT PRIMARY KEY,
    result_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS enrichment_spend (
    day TEXT PRIMARY KEY,
    amount REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS scrape_health (
    source TEXT NOT NULL,
    check_name TEXT NOT NULL,
    failing INTEGER NOT NULL,
    detail TEXT NOT NULL,
    snapshot TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_alert_at TEXT,
    PRIMARY KEY (source, check_name)
);
"""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@contextmanager
def get_db(path: str = DB_PATH):
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        for name, definition in {
            "notify_attempts": "INTEGER NOT NULL DEFAULT 0",
            "next_notify_at": "TEXT NOT NULL DEFAULT ''",
            "destination_chat_id": "TEXT",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        enrichment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_enrichments)")}
        for name, definition in {
            "job_family": "TEXT", "specialization": "TEXT", "employer_sector": "TEXT",
            "routing_confidence": "TEXT", "needs_review": "INTEGER",
            "schema_version": "TEXT NOT NULL DEFAULT ''",
        }.items():
            if name not in enrichment_columns:
                conn.execute(f"ALTER TABLE job_enrichments ADD COLUMN {name} {definition}")
        # Keep previous results for audit; never reinterpret an industry as a
        # profession or enqueue a paid historical reclassification implicitly.
        conn.execute("UPDATE job_enrichments SET state='obsolete' "
                     "WHERE state IN ('ready','fallback') AND schema_version != ?",
                     (ENRICHMENT_SCHEMA_VERSION,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_family_sector "
                     "ON job_enrichments(schema_version, job_family, employer_sector)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending_source "
                     "ON jobs(source, posted_at, id) WHERE notified = 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending "
                     "ON jobs(posted_at, id) WHERE notified = 0")


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def save_job(job: dict):
    """Compatibility wrapper for a single insert."""
    return save_jobs([job])


def save_jobs(jobs: list[dict], blocked: list[tuple[str, str]] | None = None,
              enqueue: bool | None = None) -> int:
    """Save raw jobs, dedupe IDs and queue entries in one atomic transaction.

    Only newly inserted jobs are queued. Repeated scrapes never reset delivery
    or enqueue historical jobs. No model/network work occurs in this transaction.
    """
    if enqueue is None:
        enqueue = ENRICHMENT_ENABLED
    inserted = 0
    with get_db() as conn:
        for job in jobs:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO jobs
                    (source, external_id, title, company, posted_at,
                     description, link, extra, scraped_at)
                VALUES
                    (:source, :external_id, :title, :company, :posted_at,
                     :description, :link, :extra, :scraped_at)
                """,
                {
                    "source": job["source"],
                    "external_id": str(job["external_id"]),
                    "title": job.get("title"),
                    "company": job.get("company"),
                    "posted_at": job.get("posted_at"),
                    "description": job.get("description"),
                    "link": job.get("link"),
                    "extra": json.dumps(job.get("extra") or {}, ensure_ascii=False),
                    "scraped_at": job.get("scraped_at") or now_str(),
                },
            )

            if cur.rowcount:
                inserted += 1
                if enqueue:
                    conn.execute("INSERT INTO job_enrichments (job_id, country, updated_at) VALUES (?, ?, ?)",
                                 (cur.lastrowid, job.get("country") or "EG", now_str()))
        ids = [(j["source"], str(j["external_id"])) for j in jobs]
        ids.extend(blocked or [])
        conn.executemany("INSERT OR IGNORE INTO seen_ids (source, external_id) VALUES (?, ?)", ids)
    return inserted


def load_seen_ids(source: str) -> set[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT external_id FROM seen_ids WHERE source = ?", (source,)
        )
        return {r["external_id"] for r in rows}


def seen_ids_for(source: str, external_ids) -> set[str]:
    """Check only this page's IDs using the existing composite primary key."""
    ids = list(dict.fromkeys(str(key) for key in external_ids if key))
    found = set()
    with get_db() as conn:
        for offset in range(0, len(ids), 200):
            batch = ids[offset:offset + 200]
            placeholders = ",".join("?" for _ in batch)
            found.update(row[0] for row in conn.execute(
                f"SELECT external_id FROM seen_ids WHERE source=? AND external_id IN ({placeholders})",
                [source, *batch]))
    return found


def mark_seen(source: str, external_id: str):
    """Record an id as seen WITHOUT saving a job — used for jobs we
    deliberately drop (e.g. blocked companies) so they're never
    re-scraped, but also never stored or notified."""
    with get_db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_ids (source, external_id) VALUES (?, ?)",
            (source, str(external_id)),
        )


def get_unnotified(source: str | None = None, limit: int = NOTIFY_BATCH_SIZE) -> list[sqlite3.Row]:
    """Read a bounded delivery batch without loading full job descriptions."""
    with get_db() as conn:
        where = "j.notified = 0 AND j.next_notify_at <= ?"
        args = [now_str()]
        if ENRICHMENT_ENABLED:
            where += " AND (e.job_id IS NULL OR (e.state IN ('ready','fallback') AND e.schema_version=?))"
            args.append(ENRICHMENT_SCHEMA_VERSION)
        if source:
            where += " AND j.source = ?"
            args.append(source)
        args.extend((NOTIFY_PER_CHANNEL_LIMIT, max(0, limit)))
        return conn.execute(
            "SELECT * FROM (SELECT j.id, j.source, j.title, j.company, j.posted_at, j.link, j.extra,"
            " j.destination_chat_id, e.job_family, e.employer_sector, e.country, e.needs_review, ROW_NUMBER() OVER (PARTITION BY "
            " COALESCE(j.destination_chat_id, e.job_family, 'legacy')"
            " ORDER BY j.posted_at, j.id) AS channel_rank FROM jobs j"
            " LEFT JOIN job_enrichments e ON e.job_id = j.id WHERE " + where +
            ") WHERE channel_rank <= ? ORDER BY channel_rank, posted_at, id LIMIT ?", args,
        ).fetchall()


def mark_notified(job_id: int):
    with get_db() as conn:
        conn.execute("UPDATE jobs SET notified = 1 WHERE id = ?", (job_id,))


def set_destination(job_id: int, chat_id: str):
    with get_db() as conn:
        conn.execute("UPDATE jobs SET destination_chat_id = COALESCE(destination_chat_id, ?) WHERE id = ?",
                     (str(chat_id), job_id))


def defer_notification(job_id: int, minimum_delay: float = 0):
    with get_db() as conn:
        row = conn.execute("SELECT notify_attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row:
            seconds = max(minimum_delay, min(3600, 60 * 2 ** min(row[0], 6)))
            retry_at = (datetime.now() + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("UPDATE jobs SET notify_attempts = notify_attempts + 1, next_notify_at = ? WHERE id = ?",
                         (retry_at, job_id))


def pending_enrichments(limit: int) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT j.*, e.attempts FROM job_enrichments e JOIN jobs j ON j.id=e.job_id"
            " WHERE e.state='pending' AND e.next_attempt_at <= ? ORDER BY e.job_id LIMIT ?",
            (now_str(), limit),
        ).fetchall()


def cached_enrichment(input_hash: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT result_json FROM enrichment_cache WHERE input_hash=?", (input_hash,)).fetchone()
        return json.loads(row[0]) if row else None


def finish_enrichment(job_id: int, result: dict, input_hash: str, model: str,
                      version: str, fallback: bool = False, error: str = "", usage: dict | None = None):
    payload = json.dumps(result, ensure_ascii=False)
    classification = result["classification"]
    with get_db() as conn:
        conn.execute("UPDATE job_enrichments SET state=?, job_family=?, specialization=?, employer_sector=?,"
                     " routing_confidence=?, needs_review=?, schema_version=?, result_json=?, input_hash=?,"
                     " model=?, version=?, error=?, usage_json=?, updated_at=? WHERE job_id=?",
                     ("fallback" if fallback else "ready", classification["job_family"],
                      classification["specialization"], classification["employer_sector"],
                      classification["routing_confidence"], int(classification["needs_review"]), ENRICHMENT_SCHEMA_VERSION,
                      payload, input_hash, model, version, error, json.dumps(usage or {}), now_str(), job_id))
        if not fallback:
            conn.execute("INSERT OR REPLACE INTO enrichment_cache VALUES (?, ?, ?)",
                         (input_hash, payload, now_str()))


def retry_enrichment(job_id: int, attempts: int, error: str):
    retry_at = (datetime.now() + timedelta(seconds=min(3600, 60 * 2 ** attempts))).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("UPDATE job_enrichments SET attempts=?, next_attempt_at=?, error=?, updated_at=? WHERE job_id=?",
                     (attempts, retry_at, error, now_str(), job_id))


def reserve_enrichment_spend(day: str, amount: float, budget: float) -> bool:
    """Reserve a conservative upper bound before sending a paid request."""
    with get_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT OR IGNORE INTO enrichment_spend(day) VALUES (?)", (day,))
        current = conn.execute("SELECT amount FROM enrichment_spend WHERE day=?", (day,)).fetchone()[0]
        if current + amount > budget:
            return False
        conn.execute("UPDATE enrichment_spend SET amount=amount+? WHERE day=?", (amount, day))
        return True


def reconcile_enrichment_spend(day: str, reserved: float, actual: float):
    with get_db() as conn:
        conn.execute("UPDATE enrichment_spend SET amount=MAX(0,amount+?) WHERE day=?", (actual-reserved, day))


# ---------------------------------------------------------------------------
# Runs (audit)
# ---------------------------------------------------------------------------


def start_run(source: str) -> int:
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO runs (source, status, started_at) VALUES (?, 'running', ?)",
            (source, now_str()),
        )
        return cur.lastrowid


def finish_run(run_id: int, status: str, jobs_found: int = 0, error: str = ""):
    with get_db() as conn:
        conn.execute(
            "UPDATE runs SET status = ?, jobs_found = ?, error = ?,"
            " finished_at = ? WHERE id = ?",
            (status, jobs_found, error, now_str(), run_id),
        )


# ---------------------------------------------------------------------------
# Login state (key/value)
# ---------------------------------------------------------------------------


def get_state(key: str, default: str = "") -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT value FROM login_state WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else default


def set_state(key: str, value):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO login_state (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )
