"""SQLite persistence: jobs, seen_ids, runs, and login state."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime

from config import DB_PATH, MAX_JOBS

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
        conn.executescript(_SCHEMA)


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def save_job(job: dict):
    """Insert job and mark its id as seen. Prune jobs table to MAX_JOBS."""
    with get_db() as conn:
        conn.execute(
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

        conn.execute(
            "INSERT OR IGNORE INTO seen_ids (source, external_id) VALUES (?, ?)",
            (job["source"], str(job["external_id"])),
        )

        conn.execute(
            """
            DELETE FROM jobs WHERE id NOT IN (
                SELECT id FROM jobs
                ORDER BY scraped_at DESC, id DESC
                LIMIT ?
            )
            """,
            (MAX_JOBS,),
        )


def load_seen_ids(source: str) -> set[str]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT external_id FROM seen_ids WHERE source = ?", (source,)
        ).fetchall()
        return {r["external_id"] for r in rows}


def get_unnotified(source: str | None = None) -> list[sqlite3.Row]:
    with get_db() as conn:
        if source:
            return conn.execute(
                "SELECT * FROM jobs WHERE notified = 0 AND source = ?"
                " ORDER BY posted_at ASC",
                (source,),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM jobs WHERE notified = 0 ORDER BY posted_at ASC"
        ).fetchall()


def mark_notified(job_id: int):
    with get_db() as conn:
        conn.execute("UPDATE jobs SET notified = 1 WHERE id = ?", (job_id,))


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
