"""Reliability + latency report for one time window, for baseline-vs-branch comparisons.

Read-only. Same metrics for any window so two deployments can be compared:

  python scripts/report_window.py --since "2026-10-09 16:50" --until "2026-10-10 08:00"

Times are LOCAL (TZ, as shown to the user); the DB stores UTC. Inside the
container: `docker compose exec -T enrichment python - --since ... < scripts/report_window.py`.

Reliability first (never miss a job): run outcomes, coverage gaps between good
runs, and first-page saturation (a page that came back almost all new may have
had more new jobs below it). Then latency: cycle and board durations (quiet vs
busy runs), stage medians, and scraped->delivered per job, plus posted->delivered
for Wuzzuf (its postedAt is exact).
"""

import argparse
import os
import sqlite3
import statistics
from datetime import datetime, timedelta, timezone

PAGE = {"linkedin": 25, "wuzzuf": 15, "indeed": 15}
GOOD = ("ok", "degraded")


def _utc(local: str) -> str:
    """Local wall time -> stored UTC string."""
    moment = datetime.fromisoformat(local).astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _local(utc: str) -> str:
    moment = datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).astimezone()
    return moment.strftime("%m-%d %H:%M")


def _secs(start: str, end: str) -> float:
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()


def _stats(values) -> str:
    values = sorted(v for v in values if v is not None)
    if not values:
        return "n=0"
    pick = lambda q: values[min(len(values) - 1, int(len(values) * q))]
    return (f"n={len(values)} p50={statistics.median(values):.1f} p90={pick(.9):.1f} "
            f"p99={pick(.99):.1f} max={values[-1]:.1f}")


def report(conn, since: str, until: str):
    window = (since, until)
    print(f"Window {_local(since)} .. {_local(until)} (local)")

    cycles = conn.execute(
        "SELECT started_at, finished_at, status FROM scrape_batches"
        " WHERE started_at>=? AND started_at<? ORDER BY started_at", window).fetchall()
    done = [(s, f, st) for s, f, st in cycles if f]
    statuses = {}
    for _, _, st in cycles:
        statuses[st] = statuses.get(st, 0) + 1
    print(f"\nCycles: {len(cycles)} {statuses}")
    print("  duration s:", _stats(_secs(s, f) for s, f, _ in done))
    starts = [datetime.fromisoformat(s) for s, _, _ in cycles]
    print("  gap between starts s:", _stats((b - a).total_seconds() for a, b in zip(starts, starts[1:])))

    print("\nReliability per board")
    for source, page in PAGE.items():
        runs = conn.execute(
            "SELECT started_at, finished_at, status, jobs_found FROM runs"
            " WHERE source=? AND started_at>=? AND started_at<? ORDER BY started_at",
            (source, *window)).fetchall()
        if not runs:
            continue
        outcome = {}
        for _, _, st, _ in runs:
            outcome[st] = outcome.get(st, 0) + 1
        good = [(s, n or 0) for s, _, st, n in runs if st in GOOD]
        gaps = []
        for (a, _), (b, n) in zip(good, good[1:]):
            minutes = _secs(a, b) / 60
            if minutes > 10:
                gaps.append(f"{_local(a)} +{minutes:.0f}m (next run {n} new)")
        saturated = [f"{_local(s)} {n}/{page}" for s, n in good if n >= page * 0.8]
        found = [n for _, n in good]
        print(f"  {source:9s} runs={len(runs)} {outcome}")
        print(f"            new jobs={sum(found)} per-run max={max(found) if found else 0}/{page};"
              f" saturated (>=80% new)={saturated or 'none'}")
        print(f"            coverage gaps >10m: {gaps or 'none'}")

    print("\nLatency per board (run seconds)")
    for source in PAGE:
        runs = conn.execute(
            "SELECT started_at, finished_at, jobs_found FROM runs WHERE source=? AND status IN ('ok','degraded')"
            " AND started_at>=? AND started_at<? AND finished_at IS NOT NULL", (source, *window)).fetchall()
        quiet = [_secs(s, f) for s, f, n in runs if not n]
        busy = [(_secs(s, f), n) for s, f, n in runs if n]
        per_job = [(sec - statistics.median(quiet)) / n for sec, n in busy] if quiet and busy else []
        print(f"  {source:9s} quiet: {_stats(quiet)}")
        print(f"            busy:  {_stats(sec for sec, _ in busy)}; extra s per new job: {_stats(per_job)}")

    print("\nStage medians (latency_events)")
    rows = conn.execute("SELECT source, stage, seconds FROM latency_events WHERE recorded_at>=? AND recorded_at<?",
                        window).fetchall()
    groups = {}
    for source, stage, seconds in rows:
        groups.setdefault((source, stage), []).append(seconds)
    for (source, stage), values in sorted(groups.items(), key=lambda kv: -sum(kv[1]))[:18]:
        print(f"  {source:10s} {stage:22s} {_stats(values)} total={sum(values):.0f}")

    print("\nEnd to end")
    jobs = conn.execute("SELECT source, posted_at, scraped_at, notified_at, reposted_at FROM jobs"
                        " WHERE scraped_at>=? AND scraped_at<?", window).fetchall()
    fresh = [j for j in jobs if not j[4]]
    delivered = [j for j in fresh if j[3]]
    print(f"  new jobs={len(fresh)} delivered={len(delivered)} undelivered={len(fresh) - len(delivered)}")
    for source in PAGE:
        mine = [j for j in delivered if j[0] == source]
        if mine:
            print(f"  {source:9s} scraped->delivered s: {_stats(_secs(j[2], j[3]) for j in mine)}")
    exact = []
    for source, posted, _, notified, _ in delivered:
        if source != "wuzzuf" or not posted or len(posted) < 16:
            continue
        posted_utc = datetime.fromisoformat(posted).astimezone(timezone.utc).replace(tzinfo=None)
        exact.append((datetime.fromisoformat(notified) - posted_utc).total_seconds())
    if exact:
        print(f"  wuzzuf posted->delivered s: {_stats(exact)}")

    opened = conn.execute("SELECT source, check_name, COUNT(*) FROM scrape_health WHERE last_alert_at>=? AND last_alert_at<?"
                          " GROUP BY source, check_name", window).fetchall()
    print(f"\nAlerted health checks in window: {opened or 'none'}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--since", required=True, help="local time, e.g. '2026-10-09 16:50'")
    parser.add_argument("--until", default="", help="local time (default: now)")
    parser.add_argument("--db", default=os.path.join(os.environ.get("DATA_DIR", "/data"), "rtjobs.db"))
    args = parser.parse_args()
    until = _utc(args.until) if args.until else datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    report(conn, _utc(args.since), until)


if __name__ == "__main__":
    main()
