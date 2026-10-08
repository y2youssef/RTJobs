"""Rollback helper: convert stored UTC timestamps back to local wall time.

Only needed to roll back to an image older than the UTC clock (PRAGMA
user_version 0). Stop EVERY service first (scraper, enrichment, delivery,
monitor), run this once with the same TZ the containers use, then start the
old image:

  docker compose --profile enrichment stop
  docker compose run --rm -e TZ=Africa/Cairo scraper python scripts/revert_utc_timestamps.py --yes
"""
import argparse
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="")
    parser.add_argument("--yes", action="store_true", help="Confirm that all services are stopped")
    args = parser.parse_args()
    from core import db
    path = args.db or db.DB_PATH
    if not args.yes:
        parser.error("stop all services first, then pass --yes")
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            print("Database is not on the UTC clock (user_version != 1); nothing to do.")
            conn.execute("ROLLBACK")
            return 1
        for table, columns in db._UTC_COLUMNS.items():
            for column in columns:
                conn.execute(f"UPDATE {table} SET {column}=datetime({column},'localtime') "
                             f"WHERE {column} GLOB ?", (db._TIMESTAMP_GLOB,))
        conn.execute("UPDATE pipeline_state SET value=json_set(value,'$.pause_until',"
                     "datetime(json_extract(value,'$.pause_until'),'localtime')) "
                     "WHERE json_valid(value) AND json_extract(value,'$.pause_until') GLOB ?",
                     (db._TIMESTAMP_GLOB,))
        conn.execute("PRAGMA user_version = 0")
        conn.execute("COMMIT")
    finally:
        conn.close()
    print(f"Converted {path} back to local wall time (user_version 0).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
