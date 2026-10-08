"""Independent local pipeline checks with persistent Telegram alert deduplication.

python -m core.pipeline_monitor --once --dry-run
A local monitor cannot report a complete host or internet outage while offline.
"""

import argparse
from datetime import timedelta
import fcntl
import json
import logging
from pathlib import Path
import signal
import threading

import requests

import config
from core import clock, db, telegram
from core.classify import CAPACITY_PREFIX
from core.log import setup_logging

logger = logging.getLogger(__name__)


def _age(value, now):
    age = clock.age_seconds(value, now)
    return None if age is None else max(0, age)


def collect_checks(now=None, started_at=None):
    """Read bounded summaries. Missing optional job fields are never failures."""
    now = now or clock.utcnow()  # stored timestamps are naive UTC
    grace = started_at is not None and (now - started_at).total_seconds() < config.PIPELINE_STARTUP_GRACE_SECONDS
    checks = {}
    with db.get_db() as conn:
        for worker, enabled in (('enrichment', config.ENRICHMENT_ENABLED),
                                ('delivery', config.ENRICHMENT_ENABLED and config.CLASSIFIED_DELIVERY_ENABLED)):
            row = conn.execute('SELECT value,updated_at FROM pipeline_state WHERE name=?', ('worker:' + worker,)).fetchone()
            age = _age(row['updated_at'], now) if row else None
            phase = json.loads(row['value']).get('phase') if row else None
            bad = enabled and (age is None or age > config.PIPELINE_WORKER_STALE_SECONDS or phase == 'stopped')
            checks['worker_' + worker] = (bool(bad and not grace), f'{worker} heartbeat is missing, stopped or older than {config.PIPELINE_WORKER_STALE_SECONDS}s.')
        for board in ('linkedin', 'wuzzuf', 'indeed'):
            enabled = getattr(config, board.upper() + '_ENABLED')
            latest = conn.execute('SELECT status,started_at FROM runs WHERE source=? ORDER BY id DESC LIMIT 1', (board,)).fetchone()
            completed = conn.execute("SELECT status FROM runs WHERE source=? AND status!='running' ORDER BY id DESC LIMIT 1", (board,)).fetchone()
            age = _age(latest['started_at'], now) if latest else None
            checks['scraper_' + board] = (bool(enabled and not grace and (age is None or age > config.PIPELINE_SCRAPER_STALE_SECONDS)),
                f'{board}: no recent scrape start within {config.PIPELINE_SCRAPER_STALE_SECONDS}s; check scheduler, host uptime and network.')
            # Parser alerts carry snapshots. This separate check prevents a
            # recent failed scrape from being advertised as a healthy pipeline.
            checks['scrape_result_' + board] = (bool(enabled and completed and completed['status'] != 'ok'),
                f'{board}: latest completed scrape was {completed["status"] if completed else "unobserved"}; see board alerts and logs.')
        # A cycle still scraping (e.g. holding for a 2FA/code solve) is not yet
        # classifier work, and held-back cycles have their own check below.
        pending = conn.execute("SELECT COUNT(*) AS n,MIN(e.created_at) AS oldest,MAX(e.attempts) AS attempts "
            "FROM job_enrichments e LEFT JOIN scrape_batches b ON b.id=e.batch_id WHERE e.state='pending' "
            "AND (b.status IS NULL OR b.status!='running') AND COALESCE(e.error,'') NOT LIKE ?",
            (CAPACITY_PREFIX + '%',)).fetchone()
        age = _age(pending['oldest'], now)
        checks['classification_queue'] = (bool(config.ENRICHMENT_ENABLED and age is not None and age > config.PIPELINE_QUEUE_STALE_SECONDS),
            f'{pending["n"]} jobs awaiting classification; oldest wait {int(age or 0)}s. Raw jobs remain saved.')
        # Stored queue errors are bounded, sanitized local messages (schema
        # paths, job IDs, enum values, HTTP status), never provider text.
        latest = conn.execute("SELECT error FROM job_enrichments WHERE state='pending' AND attempts>=? "
            "ORDER BY updated_at DESC LIMIT 1", (config.CLASSIFIER_ALERT_AFTER_FAILURES,)).fetchone()
        checks['classification_retries'] = (bool(config.ENRICHMENT_ENABLED and (pending['attempts'] or 0) >= config.CLASSIFIER_ALERT_AFTER_FAILURES),
            f'Classification has failed at least {config.CLASSIFIER_ALERT_AFTER_FAILURES} times for pending work; '
            f'retries now back off up to {config.CLASSIFIER_RETRY_MAX_SECONDS // 60} min. '
            f'Latest error: {((latest["error"] if latest else "") or "unknown")[:300]}. No failure is routed to Other.')
        held = conn.execute("SELECT COUNT(*) AS n,MAX(error) AS error FROM job_enrichments WHERE state='pending' AND error LIKE ?",
            (CAPACITY_PREFIX + '%',)).fetchone()
        checks['classification_capacity'] = (bool(config.ENRICHMENT_ENABLED and held['n']),
            f'{held["n"]} jobs are held back without paid attempts. {(held["error"] or "")[:300]}. '
            'Raw jobs remain saved; raise the daily budget, choose a larger model, or split the cycle.')
        invalid = conn.execute("SELECT COUNT(*) AS n,MAX(error) AS error FROM job_enrichments WHERE state='input_error'").fetchone()
        checks['classification_input_errors'] = (bool(config.ENRICHMENT_ENABLED and invalid['n']),
            f'{invalid["n"]} jobs were set aside because their saved input cannot be prepared '
            f'({(invalid["error"] or "")[:200]}). They are not delivered; fix the data, then set '
            "state='pending' for state='input_error' rows (see core/db.py quarantine_enrichments).")
        delivery = conn.execute("SELECT COUNT(*) AS n,MIN(CASE WHEN e.job_id IS NULL THEN j.scraped_at ELSE e.updated_at END) AS oldest,"
            "MAX(j.notify_attempts) AS attempts FROM jobs j LEFT JOIN job_enrichments e ON e.job_id=j.id "
            "WHERE j.notified=0 AND (e.job_id IS NULL OR (e.state='ready' AND e.schema_version=?))", (config.ENRICHMENT_SCHEMA_VERSION,)).fetchone()
        age = _age(delivery['oldest'], now)
        delivery_enabled = config.ENRICHMENT_ENABLED and config.CLASSIFIED_DELIVERY_ENABLED
        checks['delivery_queue'] = (bool(delivery_enabled and age is not None and age > config.PIPELINE_QUEUE_STALE_SECONDS),
            f'{delivery["n"]} jobs ready for Telegram; oldest wait {int(age or 0)}s.')
        checks['delivery_retries'] = (bool(delivery_enabled and (delivery['attempts'] or 0) >= config.PIPELINE_DELIVERY_ALERT_ATTEMPTS),
            f'Telegram delivery has failed at least {config.PIPELINE_DELIVERY_ALERT_ATTEMPTS} times for pending jobs. Delivery will retry.')
    gate = db.get_pipeline_state('classifier')
    checks['classifier_provider'] = (bool(config.ENRICHMENT_ENABLED and gate.get('reason') != 'daily_budget_exhausted' and
        (gate.get('alert') or gate.get('failures', 0) >= config.CLASSIFIER_ALERT_AFTER_FAILURES)),
        f'Classifier provider unavailable: {gate.get("reason", "none")}. Next attempt: {clock.to_local(gate.get("pause_until")) or "not scheduled"} (local time).')
    checks['classifier_budget'] = (bool(config.ENRICHMENT_ENABLED and gate.get('reason') == 'daily_budget_exhausted' and
        gate.get('pause_until', '') > now.strftime(clock.FORMAT)),
        f'Daily classifier budget cannot cover the next whole batch. Jobs remain pending until {clock.to_local(gate.get("pause_until")) or "the next budget window"} (local time).')
    return checks


def report_checks(checks, now=None):
    """One successful alert per failure episode; retry failed sends after restart."""
    now = now or clock.utcnow()
    stamp = now.strftime(clock.FORMAT)
    for name, (failing, detail) in checks.items():
        with db.get_db() as conn:
            previous = conn.execute('SELECT * FROM pipeline_alerts WHERE check_name=?', (name,)).fetchone()
            continuing = bool(previous and previous['failing'] and failing)
            first = previous['first_seen_at'] if continuing else stamp
            alerted = previous['last_alert_at'] if continuing else None
            next_at = previous['next_alert_at'] if continuing else ''
            conn.execute('INSERT INTO pipeline_alerts VALUES (?,?,?,?,?,?,?) ON CONFLICT(check_name) DO UPDATE SET '
                'failing=excluded.failing,detail=excluded.detail,first_seen_at=excluded.first_seen_at,last_seen_at=excluded.last_seen_at,'
                'last_alert_at=excluded.last_alert_at,next_alert_at=excluded.next_alert_at',
                (name, int(failing), detail, first, stamp, alerted, next_at))
        if not failing or alerted or next_at > stamp:
            continue
        # Network I/O must never hold a SQLite write transaction.
        sent = telegram.notify_failure('Pipeline: ' + name, detail,
            hint='Check enrichment, delivery, monitor and scheduler logs. Saved raw jobs and pending work are retained.')
        retry = (now + timedelta(seconds=config.PIPELINE_ALERT_RETRY_SECONDS)).strftime(clock.FORMAT)
        with db.get_db() as conn:
            conn.execute('UPDATE pipeline_alerts SET last_alert_at=?,next_alert_at=? WHERE check_name=?',
                         (stamp if sent else None, '' if sent else retry, name))


def monitor_once(now=None, started_at=None, dry_run=False):
    now = now or clock.utcnow()
    checks = collect_checks(now=now, started_at=started_at)
    if not dry_run:
        report_checks(checks, now=now)
        starting = started_at is not None and (now - started_at).total_seconds() < config.PIPELINE_STARTUP_GRACE_SECONDS
        if config.HEALTHCHECK_URL and not starting and not any(bad for bad, _ in checks.values()):
            try:
                requests.get(config.HEALTHCHECK_URL, timeout=10).raise_for_status()
            except requests.RequestException as exc:
                logger.warning('External heartbeat failed: %s', type(exc).__name__)
    return checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--dry-run', action='store_true', help='Print checks without Telegram or external heartbeat calls')
    args = parser.parse_args()
    setup_logging()
    Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        db.init_db()
        print(json.dumps(monitor_once(dry_run=True), indent=2))
        return 0
    with open(config.DB_PATH + '.monitor.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info('Another pipeline monitor is active')
            return 0
        db.init_db()
        stop = threading.Event()
        started_at = clock.utcnow()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            while not stop.is_set():
                db.touch_worker('monitor', 'checking')
                checks = monitor_once(started_at=started_at)
                logger.info('Pipeline checks failing=%s', ','.join(name for name, (bad, _) in checks.items() if bad) or 'none')
                if args.once:
                    break
                stop.wait(config.PIPELINE_MONITOR_INTERVAL_SECONDS)
        finally:
            db.touch_worker('monitor', 'stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
