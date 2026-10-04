"""Offline integration checks for cycle batching, concurrent delivery and alerts."""
import copy
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


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='rtjobs-pipeline-test-') as directory:
        os.environ.update(PYTHON_DOTENV_DISABLED='1', DATA_DIR=directory, MARKUP_DIR=str(ROOT/'markup'), LOG_FILE='',
            TELEGRAM_TOKEN='x', TELEGRAM_CHAT_ID='1', TELEGRAM_TEST_ID='2', TELEGRAM_FAILURE_CHAT_ID='2',
            OPENROUTER_API_KEY='x', ENRICHMENT_ENABLED='true', CLASSIFIED_DELIVERY_ENABLED='true',
            TELEGRAM_CHANNELS_JSON='', CLASSIFIER_DAILY_BUDGET_USD='10', CLASSIFIER_MAX_INPUT_CHARS='0')
        import requests
        from core import db, telegram, classify, enrichment_worker as worker, pipeline_monitor as monitor
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
            with patch.object(client, 'request_bound', return_value=100):
                held = worker.process_batch(client, jobs3)
            assert all(row['state'] == 'pending' for row in held['jobs']) and held['api_calls'] == 0
            assert db.get_pipeline_state('classifier')['reason'] == 'daily_budget_exhausted'
            db.set_pipeline_state('classifier', {})
            print('PASS cycle separation, interrupted-cycle recovery, persistent provider pause and budget deferral')

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

            now = datetime.now(); later = now + timedelta(seconds=config.PIPELINE_ALERT_RETRY_SECONDS + 1)
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
                checks = monitor.collect_checks(now=datetime.now())
                assert not checks['worker_enrichment'][0] and not checks['worker_delivery'][0]
                checks = monitor.collect_checks(now=datetime.now()+timedelta(seconds=config.PIPELINE_WORKER_STALE_SECONDS+1))
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
