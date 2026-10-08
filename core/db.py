"""SQLite persistence: jobs, seen_ids, runs, and login state."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta

from config import (DB_PATH, ENRICHMENT_ENABLED, ENRICHMENT_SCHEMA_VERSION,
                    NOTIFY_BATCH_SIZE, NOTIFY_PER_CHANNEL_LIMIT, CLASSIFIER_RETRY_MAX_SECONDS)
from core import wakeup

_active_scrape_batch = None  # Set only by this process's orchestrator.

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
CREATE TABLE IF NOT EXISTS pipeline_state (
    name TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pipeline_alerts (
    check_name TEXT PRIMARY KEY,
    failing INTEGER NOT NULL,
    detail TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_alert_at TEXT,
    next_alert_at TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scrape_batches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running'
);
CREATE TABLE IF NOT EXISTS enrichment_requests (
    request_id TEXT PRIMARY KEY,
    batch_id INTEGER,
    job_ids TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    state TEXT NOT NULL DEFAULT 'running',
    usage_json TEXT,
    error TEXT
);
-- Latency instrumentation: one row per measured pipeline stage
-- (chrome cold start, login, search pages, detail panels, human delays,
-- queue pickup, model requests, delivery). Written by core/timing.py.
CREATE TABLE IF NOT EXISTS latency_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    recorded_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    stage       TEXT NOT NULL,
    seconds     REAL NOT NULL,
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_latency_stage ON latency_events(stage, recorded_at);
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
            "notified_at": "TEXT",
        }.items():
            if name not in columns:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        enrichment_columns = {row["name"] for row in conn.execute("PRAGMA table_info(job_enrichments)")}
        for name, definition in {
            "job_family": "TEXT", "specialization": "TEXT", "employer_sector": "TEXT",
            "routing_confidence": "TEXT", "needs_review": "INTEGER",
            "schema_version": "TEXT NOT NULL DEFAULT ''",
            "created_at": "TEXT",
            "batch_id": "INTEGER",
        }.items():
            if name not in enrichment_columns:
                conn.execute(f"ALTER TABLE job_enrichments ADD COLUMN {name} {definition}")
        if 'batch_id' not in {row['name'] for row in conn.execute('PRAGMA table_info(runs)')}:
            conn.execute('ALTER TABLE runs ADD COLUMN batch_id INTEGER')
        # Keep previous results for audit; never reinterpret an industry as a
        # profession or enqueue a paid historical reclassification implicitly.
        conn.execute("UPDATE job_enrichments SET state='obsolete' "
                     "WHERE state IN ('ready','fallback') AND schema_version != ?",
                     (ENRICHMENT_SCHEMA_VERSION,))
        conn.execute("UPDATE job_enrichments SET created_at=COALESCE("
                     "(SELECT scraped_at FROM jobs WHERE jobs.id=job_id), updated_at, ?) "
                     "WHERE created_at IS NULL", (now_str(),))
        # Recover only unfinished operational fallbacks from the current
        # contract. Already delivered jobs are never resent by this migration.
        conn.execute("UPDATE jobs SET destination_chat_id=NULL WHERE notified=0 AND id IN "
                     "(SELECT job_id FROM job_enrichments WHERE state='fallback' AND schema_version=?)",
                     (ENRICHMENT_SCHEMA_VERSION,))
        conn.execute("UPDATE job_enrichments SET state='pending',next_attempt_at='' "
                     "WHERE state='fallback' AND schema_version=? AND job_id IN "
                     "(SELECT id FROM jobs WHERE notified=0)", (ENRICHMENT_SCHEMA_VERSION,))
        conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_family_sector "
                     "ON job_enrichments(schema_version, job_family, employer_sector)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending_source "
                     "ON jobs(source, posted_at, id) WHERE notified = 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_pending "
                     "ON jobs(posted_at, id) WHERE notified = 0")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_source_id ON runs(source,id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_enrichment_batch ON job_enrichments(batch_id,state)")


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
                    conn.execute("INSERT INTO job_enrichments (job_id, country, updated_at, created_at, batch_id) VALUES (?, ?, ?, ?, ?)",
                                 (cur.lastrowid, job.get("country") or "EG", now_str(), now_str(), _active_scrape_batch))
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
            where += " AND (e.job_id IS NULL OR (e.state='ready' AND e.schema_version=?))"
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
        conn.execute("UPDATE jobs SET notified = 1,notified_at=? WHERE id = ?", (now_str(), job_id))


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
            "SELECT j.*, e.attempts,e.error FROM job_enrichments e JOIN jobs j ON j.id=e.job_id"
            " WHERE e.state='pending' AND e.next_attempt_at <= ? ORDER BY e.job_id LIMIT ?",
            (now_str(), limit),
        ).fetchall()


def pending_enrichment_batch() -> list[sqlite3.Row]:
    """Select every pending job from one finished cycle, with no count cap.

    Pre-migration queued rows (NULL batch_id) form one legacy queue batch. A
    retry never silently sends only the due subset of a partially deferred batch.
    """
    with get_db() as conn:
        batch = conn.execute("SELECT e.batch_id FROM job_enrichments e "
            "LEFT JOIN scrape_batches b ON b.id=e.batch_id WHERE e.state='pending' "
            "AND (e.batch_id IS NULL OR b.status!='running') GROUP BY e.batch_id "
            "HAVING MAX(e.next_attempt_at)<=? ORDER BY MIN(e.created_at),MIN(e.job_id) LIMIT 1",
            (now_str(),)).fetchone()
        if batch is None:
            return []
        return conn.execute("SELECT j.*,e.attempts,e.batch_id,e.error FROM job_enrichments e "
            "JOIN jobs j ON j.id=e.job_id WHERE e.state='pending' AND e.batch_id IS ? ORDER BY e.job_id",
            (batch[0],)).fetchall()


def start_scrape_batch() -> int:
    global _active_scrape_batch
    with get_db() as conn:
        # Ofelia serializes scraper processes. A previous unfinished cycle was
        # interrupted; release its already-saved jobs without mixing cycles.
        recovered = conn.execute("UPDATE scrape_batches SET status='interrupted',finished_at=? WHERE status='running'", (now_str(),)).rowcount
        row = conn.execute("INSERT INTO scrape_batches(started_at) VALUES (?)", (now_str(),))
        _active_scrape_batch = row.lastrowid
    if recovered and ENRICHMENT_ENABLED:
        wakeup.notify('enrichment')
    return _active_scrape_batch


def finish_scrape_batch(batch_id: int, interrupted: bool = False):
    global _active_scrape_batch
    with get_db() as conn:
        statuses = [row[0] for row in conn.execute('SELECT status FROM runs WHERE batch_id=?', (batch_id,))]
        status = 'interrupted' if interrupted else ('ok' if all(value == 'ok' for value in statuses) else 'degraded')
        conn.execute('UPDATE scrape_batches SET status=?,finished_at=? WHERE id=?', (status, now_str(), batch_id))
    _active_scrape_batch = None
    if ENRICHMENT_ENABLED:
        wakeup.notify('enrichment')


def start_enrichment_request(request_id: str, batch_id: int | None, job_ids: list[int]):
    with get_db() as conn:
        conn.execute('INSERT INTO enrichment_requests(request_id,batch_id,job_ids,started_at) VALUES (?,?,?,?)',
                     (request_id, batch_id, json.dumps(job_ids), now_str()))


def finish_enrichment_request(request_id: str, usage: dict, error: str = ''):
    with get_db() as conn:
        conn.execute('UPDATE enrichment_requests SET state=?,finished_at=?,usage_json=?,error=? WHERE request_id=?',
                     ('failed' if error else 'ready', now_str(), json.dumps(usage), error, request_id))


def forget_cached_enrichment(input_hash: str):
    with get_db() as conn:
        conn.execute('DELETE FROM enrichment_cache WHERE input_hash=?', (input_hash,))


def cached_enrichment(input_hash: str) -> dict | None:
    with get_db() as conn:
        row = conn.execute("SELECT result_json FROM enrichment_cache WHERE input_hash=?", (input_hash,)).fetchone()
        return json.loads(row[0]) if row else None


def finish_enrichment(job_id: int, result: dict, input_hash: str, model: str,
                      version: str, fallback: bool = False, error: str = "", usage: dict | None = None,
                      connection=None):
    if connection is None:
        with get_db() as conn:
            finish_enrichment(job_id, result, input_hash, model, version, fallback, error, usage, conn)
        if not fallback:
            wakeup.notify('delivery')
        return
    payload = json.dumps(result, ensure_ascii=False)
    classification = result["classification"]
    conn = connection
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

def finish_enrichment_batch(entries: list[dict], model: str, version: str,
                            request_id: str | None = None, usage: dict | None = None):
    """Publish a fully validated response atomically to the delivery worker."""
    with get_db() as conn:
        for entry in entries:
            finish_enrichment(entry['job_id'], entry['result'], entry['input_hash'], model,
                              version, usage=entry.get('usage'), connection=conn)
        if request_id:
            conn.execute("UPDATE enrichment_requests SET state='ready',finished_at=?,usage_json=? WHERE request_id=?",
                         (now_str(), json.dumps(usage or {}), request_id))
    if entries:
        wakeup.notify('delivery')


def retry_enrichment_batch(jobs: list[dict], error: str, retry_at: str, attempted: bool):
    """Defer a cycle atomically so restart cannot pick a partially updated batch."""
    with get_db() as conn:
        conn.executemany("UPDATE job_enrichments SET attempts=?,next_attempt_at=?,error=?,updated_at=? WHERE job_id=?",
                         [(job.get('attempts', 0) + int(attempted), retry_at, error, now_str(), job['id']) for job in jobs])


def quarantine_enrichments(entries: list[tuple[int, str]]):
    """Set aside jobs whose stored input cannot be prepared (never delivered).

    After fixing the data, requeue with:
    UPDATE job_enrichments SET state='pending', next_attempt_at='' WHERE state='input_error'
    """
    with get_db() as conn:
        conn.executemany("UPDATE job_enrichments SET state='input_error',error=?,updated_at=? "
                         "WHERE job_id=? AND state='pending'",
                         [(error, now_str(), job_id) for job_id, error in entries])


def retry_enrichment(job_id: int, attempts: int, error: str, retry_at: str | None = None):
    retry_at = retry_at or (datetime.now() + timedelta(seconds=min(
        CLASSIFIER_RETRY_MAX_SECONDS, 60 * 2 ** min(attempts, 10)))).strftime("%Y-%m-%d %H:%M:%S")
    with get_db() as conn:
        conn.execute("UPDATE job_enrichments SET attempts=?, next_attempt_at=?, error=?, updated_at=? WHERE job_id=?",
                     (attempts, retry_at, error, now_str(), job_id))
    return retry_at


def set_pipeline_state(name: str, value: dict):
    with get_db() as conn:
        conn.execute("INSERT INTO pipeline_state VALUES (?,?,?) ON CONFLICT(name) DO UPDATE "
                     "SET value=excluded.value,updated_at=excluded.updated_at",
                     (name, json.dumps(value), now_str()))


def get_pipeline_state(name: str) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT value FROM pipeline_state WHERE name=?", (name,)).fetchone()
    return json.loads(row[0]) if row else {}


def touch_worker(name: str, phase: str):
    set_pipeline_state("worker:" + name, {"phase": phase})


def record_latency(source: str, stage: str, seconds: float, detail: dict | None = None):
    """One latency measurement row (core/timing.py); detail stored as JSON."""
    with get_db() as conn:
        conn.execute("INSERT INTO latency_events(recorded_at, source, stage, seconds, detail) VALUES (?,?,?,?,?)",
                     (now_str(), source, stage, seconds,
                      json.dumps(detail, ensure_ascii=False) if detail else None))


def scrape_batch_finished_at(batch_id) -> str | None:
    """Cycle commit time — the anchor for enrichment queue-delay metrics."""
    if batch_id is None:
        return None
    with get_db() as conn:
        row = conn.execute("SELECT finished_at FROM scrape_batches WHERE id=?", (batch_id,)).fetchone()
        return row[0] if row else None


def oldest_undelivered_ready_at() -> str | None:
    """Oldest ready-but-undelivered timestamp — delivery queue-delay anchor."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT MIN(CASE WHEN e.job_id IS NULL THEN j.scraped_at ELSE e.updated_at END) AS oldest "
            "FROM jobs j LEFT JOIN job_enrichments e ON e.job_id=j.id "
            "WHERE j.notified=0 AND (e.job_id IS NULL OR (e.state='ready' AND e.schema_version=?))",
            (ENRICHMENT_SCHEMA_VERSION,)).fetchone()
        return row[0] if row else None


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
            "INSERT INTO runs (source, status, started_at,batch_id) VALUES (?, 'running', ?,?)",
            (source, now_str(), _active_scrape_batch),
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
