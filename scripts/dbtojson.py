"""Utility: dump jobs table to JSON (one-off, not used by the scraper)."""

import json
import logging
import sqlite3

from core.log import setup_logging

logger = logging.getLogger(__name__)


def jobs_table_to_json(db_path: str, json_path: str) -> list[dict]:
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM jobs")
    columns = [d[0] for d in cursor.description]
    rows = cursor.fetchall()
    conn.close()
    data = [dict(zip(columns, r)) for r in rows]
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)
    logger.info("Exported %s rows from %s to %s", len(data), db_path, json_path)
    return data


if __name__ == "__main__":
    setup_logging()
    jobs_table_to_json("rtjobs.db", "jobs.json")
