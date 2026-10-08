"""Offline integration checks for cycle batching, concurrent delivery and alerts."""
from datetime import datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def isolated_markup(directory) -> str:
    """Copy markup/ (minus runtime snapshots) into the test directory.

    The real markup/ is bind-mounted into production as its snapshot store;
    tests that save evidence there would mix fake snapshots with real ones and
    let the 20-per-kind pruning evict genuine production evidence.
    """
    import shutil
    target = Path(directory) / "markup"
    shutil.copytree(ROOT / "markup", target, ignore=shutil.ignore_patterns("snapshots"), dirs_exist_ok=True)
    return str(target)


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='rtjobs-pipeline-test-') as directory:
        os.environ.update(PYTHON_DOTENV_DISABLED='1', DATA_DIR=directory, MARKUP_DIR=isolated_markup(directory), LOG_FILE='',
            TELEGRAM_TOKEN='x', TELEGRAM_CHAT_ID='1', TELEGRAM_TEST_ID='2', TELEGRAM_FAILURE_CHAT_ID='2',
            OPENROUTER_API_KEY='x', ENRICHMENT_ENABLED='true', CLASSIFIED_DELIVERY_ENABLED='true',
            TELEGRAM_CHANNELS_JSON='', CLASSIFIER_DAILY_BUDGET_USD='10', CLASSIFIER_MAX_INPUT_CHARS='0')
        import requests
        from core import clock, db, telegram, classify, enrichment_worker as worker, pipeline_monitor as monitor
        from core.delivery_worker import deliver_once
        import config
        # Any forgotten mock is an immediate failure, never a real HTTP call.
        with patch.object(requests.sessions.Session, 'request', side_effect=AssertionError('Real network forbidden')):
            db.init_db()
            client = classify.Enricher()
            channels = {family: '-100'+str(8000000+i) for i, family in enumerate(client.taxonomy['job_families'])}
            classify.TELEGRAM_CHANNELS_JSON = json.dumps(channels)
            def save(n, prefix):
                db.save_jobs([{'source': 'test', 'external_id': f'{prefix}-{i}', 'title': f'Backend {prefix}-{i}',
                    'description': 'Python. '+prefix+str(i), 'company': 'Example', 'extra': {}} for i in range(n)], enqueue=True)
            def pending(): return [dict(row) for row in db.pending_enrichment_batch()]
            def batch_jobs(cycle):
                with db.get_db() as conn:
                    return [dict(row) for row in conn.execute(
                        "SELECT j.*,e.attempts,e.batch_id,e.error FROM job_enrichments e JOIN jobs j ON j.id=e.job_id "
                        "WHERE e.state='pending' AND e.batch_id=? ORDER BY e.job_id", (cycle,))]
            def output(data):
                results = []
                for job in reversed(data):
                    result = client.fallback(job['id'])
                    result['classification'].update(job_family='software_engineering', specialization='backend', routing_confidence='High', needs_review=False)
                    results.append(result)
                return results
            def response(results, reason='stop'):
                return SimpleNamespace(ok=True, json=lambda: {'choices':[{'finish_reason':reason,
                    'message':{'content': json.dumps({'jobs':results})}}], 'usage':{'cost':0.01,'prompt_tokens':500}})
            cycle = db.start_scrape_batch()
            save(20, 'linkedin')
            assert pending() == [], 'Must not classify before later boards finish'
            save(17, 'wuzzuf')
            assert pending() == []
            db.finish_scrape_batch(cycle)
            jobs = pending()
            assert len(jobs) == 37
            def post(_url, **kw):
                data = json.loads(kw['json']['messages'][1]['content'])['jobs']
                assert len(data) == 37
                return response(output(data))
            with patch.object(client, 'request_bound', return_value=0.1), patch.object(client.session, 'post', side_effect=post) as call:
                report = worker.process_batch(client, jobs)
            assert call.call_count == report['api_calls'] == 1
            assert all(row['state'] == 'ready' for row in report['jobs'])
            assert [row['result']['job_id'] for row in report['jobs']] == [job['id'] for job in jobs]
            with db.get_db() as conn:
                audit = conn.execute('SELECT * FROM enrichment_requests').fetchall()
                assert len(audit) == 1 and len(json.loads(audit[0]['job_ids'])) == 37
                assert json.loads(audit[0]['usage_json'])['cost'] == 0.01
                assert abs(conn.execute('SELECT amount FROM enrichment_spend').fetchone()[0] - .01) < 1e-8
                usages = conn.execute('SELECT usage_json FROM job_enrichments').fetchall()
                assert all('cost' not in json.loads(row[0]) for row in usages), 'Shared cost multiplied per job'
            print('PASS 37 jobs across boards -> exactly one completion, reordered IDs, atomic publication and single cost')

            # A cycle with missing output never leaks a partial ready result.
            cycle2 = db.start_scrape_batch(); save(3, 'bad'); db.finish_scrape_batch(cycle2)
            jobs2 = pending()
            with patch.object(client, 'request_bound', return_value=.1), patch.object(client.session, 'post', return_value=response(output(jobs2)[:-1])):
                report = worker.process_batch(client, jobs2)
            assert all(row['state'] == 'pending' for row in report['jobs'])
            with db.get_db() as conn:
                assert conn.execute("SELECT COUNT(*) FROM job_enrichments WHERE batch_id=? AND state='ready'", (cycle2,)).fetchone()[0] == 0
                assert conn.execute('SELECT COUNT(DISTINCT next_attempt_at) FROM job_enrichments WHERE batch_id=?', (cycle2,)).fetchone()[0] == 1
                assert abs(conn.execute('SELECT amount FROM enrichment_spend').fetchone()[0] - .02) < 1e-8
            with patch.object(client, 'request_bound', return_value=.1), patch.object(client.session, 'post', return_value=response(output(jobs2), reason='length')):
                assert all(row['state'] == 'pending' for row in worker.process_batch(client, jobs2)['jobs'])
            print('PASS incomplete/truncated output keeps whole cycle pending; paid invalid output reconciled')

            # Next cycle remains separate; interrupted saved work is recoverable.
            cycle3 = db.start_scrape_batch(); save(2, 'interrupted')
            cycle4 = db.start_scrape_batch(); save(1, 'next')
            assert len(pending()) == 2 and all(row['batch_id'] == cycle3 for row in pending())
            db.finish_scrape_batch(cycle4)
            with db.get_db() as conn:
                assert conn.execute('SELECT status FROM scrape_batches WHERE id=?', (cycle3,)).fetchone()[0] == 'interrupted'
            db.set_pipeline_state('classifier', {})
            jobs3 = pending()
            with patch.object(client, 'request_bound', return_value=.1), patch.object(client.session, 'post', side_effect=requests.Timeout('private URL must not leak')):
                failed = worker.process_batch(client, jobs3)
            assert all(row['state'] == 'pending' and 'private' not in row['error'] for row in failed['jobs'])
            fresh = classify.Enricher()
            with patch.object(fresh, 'request_bound') as pricing:
                held = worker.process_batch(fresh, jobs3)
                assert not pricing.called and held['api_calls'] == 0
            db.set_pipeline_state('classifier', {})
            # A cycle priced above the WHOLE daily budget can never run: hold
            # just that cycle (free, hourly recheck) without pausing others.
            with patch.object(client, 'request_bound', return_value=100):
                held = worker.process_batch(client, jobs3)
            assert all(row['state'] == 'pending' for row in held['jobs']) and held['api_calls'] == 0
            assert all(row['error'].startswith(classify.CAPACITY_PREFIX) and 'daily budget' in row['error'] for row in held['jobs'])
            assert db.get_pipeline_state('classifier') == {}, 'an oversized cycle must not pause every cycle'
            assert all(row['next_attempt_at'] > clock.after(50 * 60) for row in held['jobs'])
            checks = monitor.collect_checks()
            assert checks['classification_capacity'][0] and 'daily budget' in checks['classification_capacity'][1]
            assert not checks['classification_queue'][0], 'held-back work has its own alert'
            # Today's remaining budget is short, but the cycle fits a fresh day:
            # the existing pause-until-midnight behaviour applies.
            today = datetime.now().strftime('%Y-%m-%d')
            with db.get_db() as conn:
                filler = 10 - conn.execute('SELECT amount FROM enrichment_spend WHERE day=?', (today,)).fetchone()[0] - .05
            assert db.reserve_enrichment_spend(today, filler, 10)
            with patch.object(client, 'request_bound', return_value=.1):
                held = worker.process_batch(client, jobs3)
            assert held['api_calls'] == 0 and db.get_pipeline_state('classifier')['reason'] == 'daily_budget_exhausted'
            db.reconcile_enrichment_spend(today, filler, 0)
            db.set_pipeline_state('classifier', {})
            print('PASS cycle separation, interrupted-cycle recovery, persistent provider pause and budget deferral')

            # Connection-level failures (host suspend/travel drops keep-alive
            # sockets) release their whole reservation: repeated offline
            # attempts must never exhaust the daily budget.
            cycle_off = db.start_scrape_batch(); save(4, 'offline'); db.finish_scrape_batch(cycle_off)
            with db.get_db() as conn:
                offline_jobs = [dict(row) for row in conn.execute(
                    "SELECT j.*,e.attempts,e.batch_id,e.error FROM job_enrichments e "
                    "JOIN jobs j ON j.id=e.job_id WHERE e.state='pending' AND e.batch_id=?", (cycle_off,)).fetchall()]
            assert len(offline_jobs) == 4
            with db.get_db() as conn:
                before = conn.execute('SELECT amount FROM enrichment_spend').fetchone()[0]
            for attempt in range(3):
                db.set_pipeline_state('classifier', {})
                with patch.object(client, 'request_bound', return_value=.5), \
                     patch.object(client.session, 'post', side_effect=requests.ConnectionError('dropped keep-alive')), \
                     patch.object(client.session, 'close') as discarded:
                    failed = worker.process_batch(client, offline_jobs)
                assert all(row['state'] == 'pending' for row in failed['jobs'])
                assert discarded.called, 'Stale keep-alive pool must be discarded after a connection failure'
            with db.get_db() as conn:
                leaked = conn.execute('SELECT amount FROM enrichment_spend').fetchone()[0]
            assert abs(leaked - before) < 1e-8, f'Offline retries leaked budget: {before} -> {leaked}'
            assert failed['jobs'][0]['error'] == 'ConnectionError during classify'
            # Never reuse an idle keep-alive socket (the provider drops them after ~4 min).
            assert client.session.headers['Connection'] == 'close'
            chained = requests.ConnectionError(OSError('Connection aborted.', ConnectionResetError(104, 'reset https://x/?k=secret')))
            assert worker._cause_chain(chained) == 'ConnectionError<OSError<ConnectionResetError'
            db.set_pipeline_state('classifier', {})
            with patch.object(client, 'request_bound', return_value=.5), patch.object(client.session, 'post',
                    side_effect=lambda _u, **kw: response(output(json.loads(kw['json']['messages'][1]['content'])['jobs']))):
                recovered = worker.process_batch(client, offline_jobs)
            assert all(row['state'] == 'ready' for row in recovered['jobs'])
            with db.get_db() as conn:
                after = conn.execute('SELECT amount FROM enrichment_spend').fetchone()[0]
            assert abs(after - (before + .01)) < 1e-8, f'Recovery spend wrong: {after} != {before}+.01'
            print('PASS connection-level failures release reservations; recovery classifies the offline backlog')

            # Cycles that can never fit one request are held for free (no
            # global pause); a known context window clamps max_tokens.
            sized = classify.Enricher()
            sized.pricing = {'prompt': 1e-9, 'completion': 1e-9, 'request': 0}
            sized.max_completion_tokens, sized.context_length = 100_000, 10_000
            cycle5 = db.start_scrape_batch(); save(3, 'context'); db.finish_scrape_batch(cycle5)
            jobs5 = batch_jobs(cycle5)
            data5 = [sized.prepare(job)[0] for job in jobs5]
            request = sized.payload(data5)
            assert request['max_tokens'] == 10_000 - classify._optimistic_tokens(request['messages']) < 2200 * 3
            assert sized.capacity_problem(data5, 0.0, 10) == ''
            sized.context_length = 3_000  # the system prompt alone is larger
            with patch.object(sized.session, 'post', side_effect=AssertionError('a hopeless cycle must not be sent')):
                held = worker.process_batch(sized, jobs5)
            assert held['api_calls'] == 0 and all('context' in row['error'] for row in held['jobs'])
            assert db.get_pipeline_state('classifier') == {}
            sized.context_length, sized.max_completion_tokens = None, 500
            assert 'output tokens' in sized.capacity_problem(data5, 0.0, 10)
            sized.close()

            # Repeated invalid output leaves the fast (paid) correction cadence
            # after the alert threshold; the alert names the validation error.
            cycle6 = db.start_scrape_batch(); save(2, 'stubborn'); db.finish_scrape_batch(cycle6)
            for attempt in range(1, config.CLASSIFIER_ALERT_AFTER_FAILURES + 2):
                jobs6 = batch_jobs(cycle6)
                with patch.object(client, 'request_bound', return_value=.01), \
                     patch.object(client.session, 'post', return_value=response(output(jobs6)[:-1])):
                    report = worker.process_batch(client, jobs6)
                delay = -clock.age_seconds(report['jobs'][0]['next_attempt_at'])
                if attempt < config.CLASSIFIER_ALERT_AFTER_FAILURES:
                    assert delay <= config.CLASSIFIER_VALIDATION_RETRY_MAX_SECONDS + 5, (attempt, delay)
                else:
                    assert delay > config.CLASSIFIER_VALIDATION_RETRY_MAX_SECONDS + 60, (attempt, delay)
            checks = monitor.collect_checks()
            assert checks['classification_retries'][0] and 'unexpected job IDs' in checks['classification_retries'][1]

            # One job with unusable saved input is set aside (never Other,
            # never delivered); the rest of its cycle still goes in ONE request.
            cycle7 = db.start_scrape_batch(); save(3, 'mixed'); db.finish_scrape_batch(cycle7)
            jobs7 = batch_jobs(cycle7)
            jobs7[0]['extra'] = '[]'
            sent_sizes = []
            def post7(_url, **kw):
                data = json.loads(kw['json']['messages'][1]['content'])['jobs']
                sent_sizes.append(len(data))
                return response(output(data))
            with patch.object(client, 'request_bound', return_value=.01), patch.object(client.session, 'post', side_effect=post7):
                report = worker.process_batch(client, jobs7)
            assert sent_sizes == [2]
            states = {row['job_id']: row['state'] for row in report['jobs']}
            assert states.pop(jobs7[0]['id']) == 'input_error' and set(states.values()) == {'ready'}
            with db.get_db() as conn:
                assert conn.execute('SELECT state FROM job_enrichments WHERE job_id=?', (jobs7[0]['id'],)).fetchone()[0] == 'input_error'
            assert jobs7[0]['id'] not in {row['id'] for row in db.get_unnotified(limit=1000)}
            checks = monitor.collect_checks()
            assert checks['classification_input_errors'][0] and 'JSON object' in checks['classification_input_errors'][1]

            # A cycle still scraping (e.g. waiting for a 2FA solve) is not
            # classifier backlog; once finished, stale work alerts as before.
            still = db.start_scrape_batch(); save(1, 'still-scraping')
            with db.get_db() as conn:
                conn.execute("UPDATE job_enrichments SET created_at='2000-01-01 00:00:00' WHERE batch_id=?", (still,))
            assert not monitor.collect_checks()['classification_queue'][0]
            db.finish_scrape_batch(still)
            assert monitor.collect_checks()['classification_queue'][0]
            print('PASS hopeless cycles held free without global pause, bounded validation spend, input quarantine, queue alert scope')

            # Delivery can commit while a model request is actively blocked.
            entered, release = threading.Event(), threading.Event()
            result_box = []
            def blocked(data):
                entered.set()
                assert release.wait(10), 'Test did not release the fake model'
                return output(data), {'cost': .001}
            with patch.object(client, 'request_bound', return_value=.1), patch.object(client, 'classify_batch', side_effect=blocked), \
                 patch.object(telegram, '_send', return_value=True), patch.object(telegram.time, 'sleep'):
                thread = threading.Thread(target=lambda: result_box.append(worker.process_batch(client, jobs3)))
                thread.start()
                try:
                    assert entered.wait(5)
                    assert deliver_once() > 0
                    assert thread.is_alive(), 'Model should still be waiting during successful delivery'
                    with db.get_db() as conn:
                        assert conn.execute('SELECT COUNT(*) FROM jobs WHERE notified=1').fetchone()[0] > 0
                finally:
                    release.set(); thread.join(10)
                assert not thread.is_alive() and result_box
            print('PASS Telegram delivery proceeds while the next batch model request is blocked')

            # Idle delivery scans neither reload the channel map nor write a
            # heartbeat every 2 seconds (phase change or 30s only).
            from core import delivery_worker
            while db.get_unnotified():
                with patch.object(telegram, '_send', return_value=True), patch.object(telegram.time, 'sleep'):
                    deliver_once()
            delivery_worker._last_beat.update(phase=None, at=0.0)
            with patch.object(db, 'touch_worker') as beat, patch.object(classify, 'load_channels') as channels:
                for _ in range(5):
                    assert deliver_once() == 0
                assert beat.call_count == 1 and not channels.called
                delivery_worker._last_beat['at'] -= 31
                deliver_once()
                assert beat.call_count == 2
            print('PASS idle delivery scans: one query, throttled heartbeat, no channel reload')

            # Current operational fallback migrations never resend delivered jobs.
            save(2, 'migration')
            with db.get_db() as conn:
                ids = [row[0] for row in conn.execute("SELECT id FROM jobs WHERE external_id LIKE 'migration-%' ORDER BY id")]
            for job_id in ids:
                db.finish_enrichment(job_id, client.fallback(job_id), 'migration'+str(job_id), 'model', client.version, fallback=True)
                db.set_destination(job_id, '-100123')
            db.mark_notified(ids[1]); db.init_db()
            with db.get_db() as conn:
                rows = conn.execute('SELECT j.notified,j.destination_chat_id,e.state FROM jobs j JOIN job_enrichments e ON e.job_id=j.id WHERE j.id IN (?,?) ORDER BY j.id', ids).fetchall()
                assert tuple(rows[0]) == (0, None, 'pending')
                assert tuple(rows[1]) == (1, '-100123', 'fallback')
            # A model that genuinely returns Other remains a valid ready result.
            db.finish_enrichment(ids[0], client.fallback(ids[0]), 'real-other', 'model', client.version)
            assert any(row['id'] == ids[0] and row['job_family'] == 'other' for row in db.get_unnotified())
            print('PASS migration preserves delivered history and distinguishes genuine Other from provider failure')

            # Every latency stage leaves a measurable row; metrics never break the pipeline.
            sys.path.insert(0, str(ROOT / 'scripts'))
            import report_latency
            from core import timing
            with db.get_db() as conn:
                db.record_latency('test', 'search_page', 1.5, {'new_jobs': 2})
                stages = report_latency.stage_summary(conn, (clock.utcnow() - timedelta(hours=1)).strftime(clock.FORMAT))
                assert 'enrichment/classify_request' in stages and 'enrichment/publish' in stages
                assert 'test/search_page' in stages and stages['test/search_page'] == [1.5]
                assert report_latency.cycle_totals(conn, '2999-01-01 00:00:00') == []
                assert report_latency.end_to_end(conn, '2999-01-01 00:00:00') == {}
            with patch.object(db, 'record_latency', side_effect=RuntimeError('metrics DB down')):
                timing.record('test', 'broken_sink', 0.1)  # must never raise
            with timing.stage('test', 'measured_stage', {'ok': True}):
                pass
            with db.get_db() as conn:
                rows = conn.execute("SELECT source, seconds, detail FROM latency_events WHERE stage='measured_stage'").fetchall()
                assert len(rows) == 1 and rows[0][0] == 'test' and rows[0][1] >= 0
                assert json.loads(rows[0][2]) == {'ok': True}
            print('PASS latency stages are recorded, aggregated and never break the pipeline')

            now = clock.utcnow(); later = now + timedelta(seconds=config.PIPELINE_ALERT_RETRY_SECONDS + 1)
            with patch.object(telegram, 'notify_failure', side_effect=[False, True, True]) as alert:
                monitor.report_checks({'test':(True,'failure')}, now)
                monitor.report_checks({'test':(True,'failure')}, now)
                assert alert.call_count == 1
                monitor.report_checks({'test':(True,'failure')}, later)
                monitor.report_checks({'test':(True,'failure')}, later + timedelta(hours=1))
                assert alert.call_count == 2
                monitor.report_checks({'test':(False,'recovered')}, later)
                monitor.report_checks({'test':(True,'failed again')}, later)
                assert alert.call_count == 3
            with patch.object(config, 'LINKEDIN_ENABLED', False), patch.object(config, 'WUZZUF_ENABLED', False), patch.object(config, 'INDEED_ENABLED', False):
                db.touch_worker('enrichment', 'idle'); db.touch_worker('delivery', 'idle')
                checks = monitor.collect_checks(now=clock.utcnow())
                assert not checks['worker_enrichment'][0] and not checks['worker_delivery'][0]
                checks = monitor.collect_checks(now=clock.utcnow()+timedelta(seconds=config.PIPELINE_WORKER_STALE_SECONDS+1))
                assert checks['worker_enrichment'][0] and checks['worker_delivery'][0]
            with patch.object(config, 'HEALTHCHECK_URL', 'https://example.invalid/heartbeat'), \
                 patch.object(monitor, 'collect_checks', return_value={'bad':(True,'degraded')}), \
                 patch.object(monitor, 'report_checks'), patch.object(monitor.requests, 'get') as ping:
                monitor.monitor_once()
                assert not ping.called
            with patch.object(telegram, 'notify_failure') as send:
                monitor.monitor_once(dry_run=True)
                assert not send.called
            client.close(); fresh.close()
            print('PASS monitor stale heartbeats, persistent alert episodes, failed-send retry, dry run and degraded heartbeat suppression')


if __name__ == '__main__':
    main()
