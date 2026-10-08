"""Classify one complete scrape cycle per OpenRouter completion request.

python -m core.enrichment_worker --preview jobs.json
python -m core.enrichment_worker --once
Delivery runs independently in core.delivery_worker.
"""

import argparse
from datetime import datetime, timedelta
import fcntl
import json
import logging
import math
from pathlib import Path
import signal
import threading
import uuid

import requests

from config import (DB_PATH, ENRICHMENT_ENABLED, CLASSIFIER_MODEL,
                    CLASSIFIER_DAILY_BUDGET_USD, CLASSIFIER_RETRY_MAX_SECONDS,
                    CLASSIFIER_VALIDATION_RETRY_MAX_SECONDS, ENRICHMENT_POLL_SECONDS)
from core import db, timing
from core.classify import Enricher, EnrichmentError, OutputValidationError
from core.log import setup_logging
from core.wakeup import Wakeup

logger = logging.getLogger(__name__)


def _retry_time(attempts, validation=False):
    ceiling = CLASSIFIER_VALIDATION_RETRY_MAX_SECONDS if validation else CLASSIFIER_RETRY_MAX_SECONDS
    base = 15 if validation else 60
    delay = min(ceiling, base * 2 ** min(attempts, 10))
    return (datetime.now() + timedelta(seconds=delay)).strftime('%Y-%m-%d %H:%M:%S')


def _defer(jobs, error, preview, retry_at=None, attempted=True):
    """Keep the whole outstanding batch pending with one retry deadline."""
    retry_at = retry_at or _retry_time(max(job.get('attempts', 0) for job in jobs) + 1)
    if not preview:
        db.retry_enrichment_batch(jobs, error, retry_at, attempted)
    logger.warning('Enrichment deferred jobs=%s until=%s reason=%s', len(jobs), retry_at, error)
    return [{'job_id': job['id'], 'state': 'pending', 'result': None,
             'cached': False, 'error': error, 'next_attempt_at': retry_at} for job in jobs]


def _billed(usage) -> bool:
    """True when the provider reported a usable actual cost for this request."""
    actual = usage.get('cost')
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and math.isfinite(actual) and actual >= 0


def _reconcile(day, bound, usage):
    if _billed(usage):
        db.reconcile_enrichment_spend(day, bound, usage['cost'])


def process_batch(client: Enricher, jobs: list[dict], preview: bool = False) -> dict:
    """Validate all identities, make at most one completion call, then publish.

    Cached jobs need no model call. Shared API usage is recorded once per
    request; individual enrichment rows reference that request instead of
    multiplying its cost by the number of jobs.
    """
    report = {'jobs': [], 'usage': {}, 'request_id': None, 'api_calls': 0}
    if not jobs:
        return report
    prepared, cached, outstanding = {}, [], []
    try:
        if len({job['id'] for job in jobs}) != len(jobs):
            raise EnrichmentError('Duplicate input job IDs')
        for job in jobs:
            data, digest = client.prepare(job)
            prepared[job['id']] = (data, digest)
            try:
                result = db.cached_enrichment(digest)
                if result is not None:
                    result['job_id'] = job['id']
                    client.validate(result, job['id'])
            except (ValueError, TypeError, KeyError):
                db.forget_cached_enrichment(digest)
                result = None
            if result is None:
                outstanding.append(job)
            else:
                cached.append({'job_id': job['id'], 'state': 'ready', 'result': result,
                               'cached': True, 'error': ''})
    except (EnrichmentError, ValueError, TypeError, KeyError) as exc:
        report['jobs'] = _defer(jobs, 'Input preparation failed: ' + type(exc).__name__, preview)
        return report

    if cached and not preview:
        db.finish_enrichment_batch([dict(row, input_hash=prepared[row['job_id']][1]) for row in cached],
                                   CLASSIFIER_MODEL, client.version)
    rows = list(cached)
    usage, bound, day, request_id = {}, None, None, None
    gate = {} if preview else db.get_pipeline_state('classifier')
    stage = 'pricing'
    if outstanding:
        if gate.get('pause_until', '') > db.now_str():
            rows.extend(_defer(outstanding, gate['reason'], preview, gate['pause_until'], attempted=False))
        else:
            try:
                data = [prepared[job['id']][0] for job in outstanding]
                # These are bounded, safe local validation messages persisted
                # with the queue, so corrections survive worker restarts.
                feedback = next((job.get('error', '') for job in outstanding
                                 if (job.get('error') or '').startswith('Invalid enrichment:')), '')
                retry_options = {'feedback': feedback[:1000]} if feedback else {}
                bound = client.request_bound(data, **retry_options)
                day = datetime.now().strftime('%Y-%m-%d')
                if not db.reserve_enrichment_spend(day, bound, CLASSIFIER_DAILY_BUDGET_USD):
                    tomorrow = (datetime.now() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
                    retry_at = tomorrow.strftime('%Y-%m-%d %H:%M:%S')
                    if not preview:
                        db.set_pipeline_state('classifier', {'reason': 'daily_budget_exhausted',
                            'pause_until': retry_at, 'failures': 0, 'alert': True})
                    rows.extend(_defer(outstanding, 'daily_budget_exhausted', preview, retry_at, attempted=False))
                else:
                    request_id = uuid.uuid4().hex
                    report.update(request_id=request_id, api_calls=1)
                    db.start_enrichment_request(request_id, outstanding[0].get('batch_id'), [job['id'] for job in outstanding])
                    stage = 'classify'
                    logger.info('Enrichment request=%s cycle=%s jobs=%s cached_jobs=%s',
                                request_id, outstanding[0].get('batch_id'), len(outstanding), len(cached))
                    with timing.stage('enrichment', 'classify_request',
                                              {'jobs': len(outstanding),
                                               'cycle': outstanding[0].get('batch_id')}):
                        results, usage = client.classify_batch(data, **retry_options)
                    # Defend the publication boundary even if the client changes.
                    with timing.stage('enrichment', 'publish', {'jobs': len(outstanding)}):
                        try:
                            results = client.validate_batch({'jobs': results}, [job['id'] for job in outstanding])
                        except ValueError as exc:
                            raise OutputValidationError('Invalid enrichment: ' + str(exc), usage=usage) from exc
                        _reconcile(day, bound, usage)
                        entries = [dict(job_id=result['job_id'], result=result,
                                        input_hash=prepared[result['job_id']][1],
                                        usage={'request_id': request_id, 'batch_size': len(outstanding)}) for result in results]
                        if not preview:
                            db.finish_enrichment_batch(entries, CLASSIFIER_MODEL, client.version,
                                                       request_id=request_id, usage=usage)
                            db.set_pipeline_state('classifier', {})
                        else:
                            db.finish_enrichment_request(request_id, usage)
                    rows.extend(dict(job_id=result['job_id'], state='ready', result=result,
                                     cached=False, error='') for result in results)
            except (EnrichmentError, requests.RequestException, ValueError, TypeError, KeyError) as exc:
                usage = getattr(exc, 'usage', None) or usage
                if request_id:
                    # A connection-level failure proves the request never
                    # reached the provider, so release its whole reservation;
                    # repeated offline attempts must not exhaust the daily
                    # budget. Timeouts keep theirs (a timeout may still be
                    # performed and billed by the provider).
                    if isinstance(exc, requests.ConnectionError) and not _billed(usage):
                        usage = {'cost': 0}
                    _reconcile(day, bound, usage)
                status = getattr(exc, 'status_code', None) or getattr(getattr(exc, 'response', None), 'status_code', None)
                # Do not expose response text, URLs with credentials or raw jobs.
                error = (('OpenRouter HTTP ' + str(status)) if status else
                         str(exc)[:1000] if isinstance(exc, EnrichmentError) else type(exc).__name__ + ' during ' + stage)
                retry_at = _retry_time(max(job.get('attempts', 0) for job in outstanding) + 1,
                                      validation=isinstance(exc, OutputValidationError))
                if request_id:
                    db.finish_enrichment_request(request_id, usage, error)
                if not preview and (isinstance(exc, requests.RequestException) or status or stage == 'pricing'):
                    failures = gate.get('failures', 0) + 1
                    retry_at = _retry_time(10 if status in (401, 402, 403) else failures)
                    db.set_pipeline_state('classifier', {'reason': error, 'pause_until': retry_at,
                        'failures': failures, 'alert': status in (401, 402, 403)})
                rows.extend(_defer(outstanding, error, preview, retry_at))
    by_id = {row['job_id']: row for row in rows}
    report['jobs'] = [by_id[job['id']] for job in jobs]
    report['usage'] = usage
    details = usage.get('prompt_tokens_details') or {}
    logger.info('Enrichment batch jobs=%s ready=%s cached=%s calls=%s input_tokens=%s cached_input_tokens=%s cost=%s',
                len(jobs), sum(row['state'] == 'ready' for row in rows), len(cached), report['api_calls'],
                usage.get('prompt_tokens', 0), details.get('cached_tokens', 0), usage.get('cost', 0))
    return report


def process_job(client: Enricher, job: dict, preview: bool = False) -> dict:
    """Explicit single-job compatibility helper; production uses process_batch."""
    report = process_batch(client, [job], preview)
    return dict(report['jobs'][0], usage=report['usage'])


def _record_queue_delay(jobs: list[dict]):
    """Time the cycle waited between the scrape commit and this pickup."""
    committed = db.scrape_batch_finished_at(jobs[0].get('batch_id'))
    anchor = committed or max(str(job.get('scraped_at') or '') for job in jobs)
    try:
        wait = (datetime.now() - datetime.fromisoformat(anchor)).total_seconds() if anchor else 0
    except ValueError:
        wait = 0
    timing.record('enrichment', 'queue_delay', max(0, wait),
                  {'jobs': len(jobs), 'cycle': jobs[0].get('batch_id')})


def run_worker(client, stop, wakeup, once=False):
    """Scan on startup/commit, keeping timeout scans for recovery and retries."""
    while not stop.is_set():
        wakeup.clear()
        db.touch_worker('enrichment', 'idle')
        jobs = [dict(row) for row in db.pending_enrichment_batch()]
        if jobs:
            logger.info('Enrichment picked up cycle=%s jobs=%s', jobs[0].get('batch_id'), len(jobs))
            _record_queue_delay(jobs)
            db.touch_worker('enrichment', 'classifying')
            process_batch(client, jobs)
            db.touch_worker('enrichment', 'idle')
        if once:
            break
        if not jobs:
            wakeup.wait(stop, ENRICHMENT_POLL_SECONDS)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    parser.add_argument('--no-send', action='store_true', help='Compatibility: delivery always runs separately')
    parser.add_argument('--preview', type=Path, help='Classify JSON without saving jobs or posting to Telegram')
    parser.add_argument('--output', type=Path)
    parser.add_argument('--limit', type=int, help='Preview only; production always processes complete cycles')
    args = parser.parse_args()
    if args.limit is not None and (args.limit <= 0 or not args.preview):
        parser.error('--limit is positive and only supported with --preview')
    if not ENRICHMENT_ENABLED and not args.preview:
        print('Enrichment is disabled. Set ENRICHMENT_ENABLED=true to enable the worker.')
        return 0
    setup_logging()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH + '.enrichment.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info('Another enrichment worker is active')
            return 0
        db.init_db()
        client = Enricher()
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
        try:
            if args.preview:
                jobs = json.loads(args.preview.read_text())
                if isinstance(jobs, dict):
                    jobs = jobs.get('jobs')
                if not isinstance(jobs, list):
                    parser.error('Preview input must be a JSON list or {"jobs": [...]}')
                jobs = [dict(job, id=job.get('id', i)) for i, job in enumerate(jobs[:args.limit], 1)]
                report = process_batch(client, jobs, preview=True)
                payload = json.dumps(report, ensure_ascii=False, indent=2)
                if args.output:
                    args.output.write_text(payload + '\n')
                else:
                    print(payload)
                return int(any(row['error'] for row in report['jobs']))
            with Wakeup('enrichment') as wakeup:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: wakeup.stop(stop))
                run_worker(client, stop, wakeup, once=args.once)
        finally:
            client.close()
            if not args.preview:
                db.touch_worker('enrichment', 'stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
