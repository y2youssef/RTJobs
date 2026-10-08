"""Latency report: which stages consume the cycle time.

python scripts/report_latency.py [--hours 24] [--db PATH]

Sources: latency_events rows (core/timing.py), scrape_batches cycle times,
runs board totals, enrichment_requests model times, and per-job
scraped_at -> notified_at end-to-end delivery latency.

Run inside a pipeline container so config.DB_PATH resolves to /data:
  docker compose exec -T enrichment python scripts/report_latency.py
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Catch-up cycles chained right after an overrun start off-grid by design;
# a real container boot never takes this long.
CHAINED_AFTER_SECONDS = 60


def _pct(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    rank = min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1))))
    return ordered[rank]


def _fmt(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


def _stats(values: list[float]) -> str:
    return (f"n={len(values)} total={_fmt(sum(values))} mean={_fmt(sum(values) / len(values))} "
            f"p50={_fmt(_pct(values, 50))} p95={_fmt(_pct(values, 95))} max={_fmt(max(values))}")


def stage_summary(conn: sqlite3.Connection, since: str) -> dict[str, list[float]]:
    """Per (source, stage) monotonic seconds since `since`. Importable for tests."""
    rows = conn.execute("SELECT source, stage, seconds FROM latency_events WHERE recorded_at >= ?", (since,)).fetchall()
    groups: dict[str, list[float]] = {}
    for source, stage, seconds in rows:
        groups.setdefault(f"{source}/{stage}", []).append(float(seconds))
    return groups


def end_to_end(conn: sqlite3.Connection, since: str) -> dict[str, list[float]]:
    """Per-source saved -> delivered seconds for jobs notified since `since`."""
    rows = conn.execute(
        "SELECT source, scraped_at, notified_at FROM jobs "
        "WHERE notified=1 AND notified_at <> '' AND scraped_at <> '' AND notified_at >= ?", (since,)).fetchall()
    groups: dict[str, list[float]] = {}
    for source, scraped_at, notified_at in rows:
        try:
            gap = (datetime.fromisoformat(notified_at) - datetime.fromisoformat(scraped_at)).total_seconds()
        except ValueError:
            continue
        if gap >= 0:
            groups.setdefault(source, []).append(gap)
    return groups


def scheduler_alignment(conn: sqlite3.Connection, since: str, grid_seconds: int = 180) -> list[float]:
    """Per-cycle delay between ofelia's grid tick and the cycle start
    (excluding cycles chained after an overrun)."""
    rows = conn.execute("SELECT started_at FROM scrape_batches WHERE started_at >= ?", (since,)).fetchall()
    gaps = []
    for (started_at,) in rows:
        try:
            moment = datetime.fromisoformat(started_at)
        except ValueError:
            continue
        stamp = moment.replace(tzinfo=timezone.utc).timestamp()  # stored timestamps are UTC
        gap = stamp - (stamp // grid_seconds) * grid_seconds
        if gap < CHAINED_AFTER_SECONDS:
            gaps.append(gap)
    return gaps


def cycle_totals(conn: sqlite3.Connection, since: str) -> list[float]:
    """Full cycle durations (commit - start) for completed cycles."""
    rows = conn.execute(
        "SELECT started_at, finished_at FROM scrape_batches "
        "WHERE started_at >= ? AND finished_at IS NOT NULL", (since,)).fetchall()
    totals = []
    for started_at, finished_at in rows:
        try:
            totals.append((datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)).total_seconds())
        except ValueError:
            continue
    return totals


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--db", default="")
    args = parser.parse_args()
    import config
    from core import clock

    path = args.db or config.DB_PATH
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    since = (clock.utcnow() - timedelta(hours=args.hours)).strftime(clock.FORMAT)

    print(f"== Latency report, last {args.hours:g}h (since {clock.to_local(since)} local) ==")
    stages = stage_summary(conn, since)
    if stages:
        print("\n-- measured stages --")
        totals = {name: sum(values) for name, values in stages.items()}
        for name in sorted(totals, key=totals.get, reverse=True):
            print(f"{name}: {_stats(stages[name])}")
    else:
        print("\nNo latency_events rows yet (upgrade deployed but no run completed).")

    totals = cycle_totals(conn, since)
    if totals:
        print(f"\n-- full scrape cycle --\nall boards: {_stats(totals)}")
    gaps = scheduler_alignment(conn, since, config.SCRAPE_INTERVAL_MINUTES * 60)
    if gaps:
        print(f"\n-- scheduler -> cycle start (docker start + xvfb + imports) --\ncontainer_boot: {_stats(gaps)}")

    e2e = end_to_end(conn, since)
    if e2e:
        print("\n-- saved -> delivered per job --")
        for source in sorted(e2e):
            print(f"{source}: {_stats(e2e[source])}")
    humans = stages.get("scraper/human_delay", []) + stages.get("linkedin/human_delay", []) + stages.get("indeed/human_delay", [])
    boards = [v for name, values in stages.items() if name.endswith("/board_run") for v in values]
    if humans and boards:
        print(f"\n-- human pacing share --\nhuman_delay total={_fmt(sum(humans))} vs board_run total={_fmt(sum(boards))}")
    usage = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(json_extract(usage_json, '$.cost')), 0) FROM enrichment_requests "
        "WHERE started_at >= ? AND state='ready'", (since,)).fetchone()
    if usage and usage[0]:
        print(f"\n-- classifier cost --\n{usage[0]} finished requests, actual billed total=${usage[1]:.4f}")
    detail = conn.execute("SELECT detail FROM latency_events WHERE stage='classify_request' AND recorded_at >= ? "
                          "ORDER BY recorded_at DESC LIMIT 1", (since,)).fetchone()
    if detail and detail[0]:
        try:
            print(f"latest batch context: {json.dumps(json.loads(detail[0]))}")
        except ValueError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
