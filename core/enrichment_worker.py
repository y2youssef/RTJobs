"""Drain SQLite enrichment work without keeping any browser alive.

python -m core.enrichment_worker --preview jobs.json --limit 3
python -m core.enrichment_worker --once --no-send
python -m core.enrichment_worker
"""

import argparse
from datetime import datetime
import fcntl
import json
import logging
import math
from pathlib import Path
import signal
import threading

import requests

from config import (DB_PATH, ENRICHMENT_ENABLED, CLASSIFIED_DELIVERY_ENABLED, CLASSIFIER_MODEL,
                    CLASSIFIER_DAILY_BUDGET_USD, CLASSIFIER_MAX_ATTEMPTS,
                    ENRICHMENT_BATCH_SIZE, ENRICHMENT_POLL_SECONDS)
from core import db
from core.classify import Enricher, EnrichmentError, load_channels
from core.log import setup_logging

logger = logging.getLogger(__name__)


def process_job(client: Enricher, job: dict, preview: bool = False) -> dict:
    """Finish, defer, or explicitly fall back; never delete a raw job."""
    digest = ""
    usage = {}
    fallback = False
    error = ""
    cached = False
    try:
        data, digest = client.prepare(job)
        result = db.cached_enrichment(digest)
        if result:
            result["job_id"] = job["id"]
            client.validate(result, job["id"])
            cached = True
        else:
            bound = client.request_bound(data)
            day = datetime.now().strftime("%Y-%m-%d")
            if not db.reserve_enrichment_spend(day, bound, CLASSIFIER_DAILY_BUDGET_USD):
                result = client.fallback(job["id"])
                fallback, error = True, "daily_budget_exhausted"
            else:
                result, usage = client.classify(data)
                actual = usage.get("cost")
                if isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and actual >= 0:
                    db.reconcile_enrichment_spend(day, bound, actual)
    except (EnrichmentError, requests.RequestException, ValueError, TypeError, KeyError) as exc:
        # Exception text never includes the request headers or raw posting.
        error = str(exc)[:300]
        attempts = job.get("attempts", 0) + 1
        if not preview and attempts < CLASSIFIER_MAX_ATTEMPTS:
            db.retry_enrichment(job["id"], attempts, error)
            logger.warning("Enrichment job %s deferred: %s", job["id"], error)
            return {"job_id": job["id"], "state": "pending", "error": error}
        result = client.fallback(job["id"])
        fallback = True

    if not preview:
        db.finish_enrichment(job["id"], result, digest, CLASSIFIER_MODEL,
                             client.version, fallback=fallback, error=error, usage=usage)
    details = usage.get("prompt_tokens_details") or {}
    logger.info("Enrichment job=%s job_family=%s result_cache=%s fallback=%s "
                "tokens=%s input_tokens=%s cached_input_tokens=%s cache_write_tokens=%s cost=%s",
                job["id"], result["classification"]["job_family"], cached, fallback,
                usage.get("total_tokens", 0), usage.get("prompt_tokens", 0),
                details.get("cached_tokens", 0), details.get("cache_write_tokens", 0),
                usage.get("cost", "reserved" if error else 0))
    return {"job_id": job["id"], "state": "fallback" if fallback else "ready",
            "result": result, "usage": usage, "cached": cached, "error": error}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--no-send", action="store_true", help="Enrich only; leave deliveries pending")
    parser.add_argument("--preview", type=Path, help="Classify a JSON list without saving jobs or sending messages")
    parser.add_argument("--output", type=Path, help="Preview report file")
    parser.add_argument("--limit", type=int, default=ENRICHMENT_BATCH_SIZE)
    args = parser.parse_args()
    if args.limit <= 0:
        parser.error("--limit must be positive")
    if not ENRICHMENT_ENABLED and not args.preview:
        print("Enrichment is disabled. Set ENRICHMENT_ENABLED=true to enable the worker.")
        return 0
    setup_logging()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    # One worker owns model calls AND notifications. The scraper can continue
    # inserting raw jobs concurrently; SQLite is never locked during network IO.
    with open(DB_PATH + ".enrichment.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info("Another enrichment worker is active")
            return 0
        db.init_db()
        channels = load_channels(require_complete=CLASSIFIED_DELIVERY_ENABLED and not (args.preview or args.no_send))
        client = Enricher()
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            if args.preview:
                jobs = json.loads(args.preview.read_text())
                if isinstance(jobs, dict):
                    jobs = jobs.get("jobs")
                if not isinstance(jobs, list):
                    parser.error('Preview input must be a JSON list or {"jobs": [...]}')
                report = []
                for i, job in enumerate(jobs[:args.limit], 1):
                    job = dict(job)
                    job.setdefault("id", i)
                    row = process_job(client, job, preview=True)
                    family = row["result"]["classification"]["job_family"]
                    row.update(title=job.get("title"), company=job.get("company"),
                               destination=channels.get(family), channel_missing=family not in channels, model=CLASSIFIER_MODEL,
                               version=client.version)
                    report.append(row)
                payload = json.dumps(report, ensure_ascii=False, indent=2)
                if args.output:
                    args.output.write_text(payload + "\n")
                else:
                    print(payload)
                return int(any(row["error"] for row in report))
            while not stop.is_set():
                for row in db.pending_enrichments(args.limit):
                    if stop.is_set():
                        break
                    process_job(client, dict(row))
                if CLASSIFIED_DELIVERY_ENABLED and not args.no_send and not stop.is_set():
                    from core.telegram import notify_jobs
                    notify_jobs(db.get_unnotified())
                if args.once:
                    break
                stop.wait(ENRICHMENT_POLL_SECONDS)
        finally:
            client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
