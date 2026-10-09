"""Real local IPC tests for immediate dispatch and durable SQLite recovery."""

import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]

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

sys.path.insert(0, str(ROOT))


def main():
    logging.disable(logging.CRITICAL)
    with tempfile.TemporaryDirectory(prefix='rtjobs-wake-') as directory:
        os.environ.update(PYTHON_DOTENV_DISABLED='1', BOARDS_PARALLEL='false', PYTHONPATH=str(ROOT), DATA_DIR=directory,
            MARKUP_DIR=isolated_markup(directory), LOG_FILE='', TELEGRAM_TOKEN='x', TELEGRAM_CHAT_ID='1',
            OPENROUTER_API_KEY='x', ENRICHMENT_ENABLED='true', CLASSIFIED_DELIVERY_ENABLED='true',
            TELEGRAM_CHANNELS_JSON='')
        from core import db, wakeup, enrichment_worker as enrichment, delivery_worker as delivery, telegram, classify
        with patch('requests.sessions.Session.request', side_effect=AssertionError('Real HTTP forbidden')):
            db.init_db()
            client = classify.Enricher()
            classify.TELEGRAM_CHANNELS_JSON = json.dumps({family:'-100'+str(8000000+i)
                for i, family in enumerate(client.taxonomy['job_families'])})
            def new_cycle(label):
                cycle = db.start_scrape_batch()
                db.save_jobs([{'source':'test','external_id':label,'title':label,'description':'Evidence '+label}], enqueue=True)
                return cycle
            def pending():return [dict(row) for row in db.pending_enrichment_batch()]

            # No listener must not turn a saved complete cycle into a failure.
            absent = new_cycle('absent')
            db.finish_scrape_batch(absent)
            assert not wakeup.notify('enrichment') and len(pending()) == 1
            with wakeup.Wakeup('enrichment') as receiver:
                assert receiver.socket is not None, 'Local IPC unavailable; run this check with socket permission'
                with patch.object(enrichment, 'process_batch') as process:
                    enrichment.run_worker(client, threading.Event(), receiver, once=True)
                    assert process.call_count == 1 and len(process.call_args.args[1]) == 1
            # Keep the isolated test queue clean without publishing messages.
            with db.get_db() as conn:
                conn.execute("UPDATE job_enrichments SET state='obsolete'")
            print('PASS stopped-worker notification loss recovered by immediate startup scan')

            cycle = new_cycle('instant')
            received, waiting, stop = threading.Event(), threading.Event(), threading.Event()
            with wakeup.Wakeup('enrichment') as receiver:
                assert receiver.socket is not None
                real_wait = receiver.wait
                def wait(stop, timeout):
                    waiting.set()  # The queue scan has completed; signal can race with select.
                    return real_wait(stop, timeout)
                def process(_client, jobs):
                    assert len(jobs) == 1 and jobs[0]['batch_id'] == cycle
                    received.set(); stop.set()
                with patch.object(receiver, 'wait', side_effect=wait), patch.object(enrichment, 'ENRICHMENT_POLL_SECONDS', 60), \
                     patch.object(enrichment, 'process_batch', side_effect=process) as call:
                    thread = threading.Thread(target=enrichment.run_worker, args=(client,stop,receiver))
                    thread.start()
                    try:
                        assert waiting.wait(2) and not call.called, 'Running cycle must not dispatch'
                        started = time.monotonic()
                        # Separate producer process exercises the actual shared-volume handoff.
                        subprocess.run([sys.executable, '-c', 'from core import db; db.finish_scrape_batch('+str(cycle)+')'],
                                       cwd=ROOT, check=True, timeout=5)
                        assert received.wait(2), 'Commit waited for the 60-second fallback'
                        assert time.monotonic() - started < 3
                        assert call.call_count == 1
                    finally:
                        receiver.stop(stop); thread.join(3)
                        assert not thread.is_alive()
            print('PASS complete-cycle commit wakes idle classifier across processes without polling delay')

            # Verify visibility at the actual notification boundary and immediate delivery.
            row = pending()[0]
            result = client.fallback(row['id'])
            result['classification'].update(job_family='software_engineering',specialization='backend',routing_confidence='High',needs_review=False)
            waiting, sent, stop = threading.Event(), threading.Event(), threading.Event()
            with wakeup.Wakeup('delivery') as receiver:
                real_wait, real_notify = receiver.wait, wakeup.notify
                def wait(stop, timeout):
                    waiting.set(); return real_wait(stop, timeout)
                def notify(name):
                    with db.get_db() as conn:
                        assert conn.execute('SELECT state FROM job_enrichments WHERE job_id=?',(row['id'],)).fetchone()[0] == 'ready'
                    return real_notify(name)
                def send(*_args, **_kwargs):
                    sent.set(); stop.set(); return True
                with patch.object(receiver,'wait',side_effect=wait), patch.object(delivery,'DELIVERY_POLL_SECONDS',60), \
                     patch.object(wakeup,'notify',side_effect=notify), patch.object(telegram,'_send',side_effect=send) as transport:
                    thread=threading.Thread(target=delivery.run_worker,args=(stop,receiver));thread.start()
                    try:
                        assert waiting.wait(2) and not transport.called
                        db.finish_enrichment_batch([{'job_id':row['id'],'result':result,'input_hash':'instant-result'}], 'test', client.version)
                        assert sent.wait(2), 'Ready results waited for the delivery fallback'
                    finally:
                        receiver.stop(stop);thread.join(3)
                        assert not thread.is_alive()
                    assert transport.call_count == 1
            with db.get_db() as conn:
                assert conn.execute('SELECT notified FROM jobs WHERE id=?',(row['id'],)).fetchone()[0] == 1
            print('PASS notification follows committed results; delivery wakes immediately and acknowledges once')

            # A producer crash after commit, before notification, remains recoverable.
            missed = new_cycle('missed')
            with wakeup.Wakeup('enrichment') as receiver:
                real_wait=receiver.wait
                waiting, received, stop=threading.Event(),threading.Event(),threading.Event()
                def wait(stop, timeout):waiting.set();return real_wait(stop, timeout)
                def process(_client,jobs):
                    assert jobs[0]['batch_id'] == missed
                    received.set();stop.set()
                with patch.object(receiver,'wait',side_effect=wait), patch.object(enrichment,'ENRICHMENT_POLL_SECONDS',0.05), \
                     patch.object(enrichment,'process_batch',side_effect=process), patch.object(wakeup,'notify',return_value=False):
                    thread=threading.Thread(target=enrichment.run_worker,args=(client,stop,receiver));thread.start()
                    try:
                        assert waiting.wait(2)
                        db.finish_scrape_batch(missed)
                        assert received.wait(2), 'Missed hint stranded SQLite work'
                    finally:
                        stop.set();thread.join(3)
                        assert not thread.is_alive()
                # Stop a real long wait promptly through its own local hint.
                stop=threading.Event()
                thread=threading.Thread(target=receiver.wait,args=(stop,60));thread.start()
                receiver.stop(stop);thread.join(2)
                assert not thread.is_alive()
            print('PASS missed-notification fallback and immediate shutdown while waiting')

            # Notifications must not bypass retry deadlines or resurrect delivered work.
            jobs=pending()
            db.retry_enrichment_batch(jobs,'deferred','2999-01-01 00:00:00',True)
            with wakeup.Wakeup('enrichment') as receiver:
                for _ in range(30):wakeup.notify('enrichment')  # Full buffer is nonblocking.
                with patch.object(enrichment,'process_batch') as process:
                    enrichment.run_worker(client,threading.Event(),receiver,once=True)
                    assert not process.called
            with wakeup.Wakeup('delivery') as receiver:
                wakeup.notify('delivery')
                with patch.object(telegram,'_send') as send:
                    delivery.run_worker(threading.Event(),receiver,once=True)
                    assert not send.called
            print('PASS coalesced/full notifications preserve backoff and do not redeliver acknowledged jobs')

            path=wakeup._path('enrichment')
            stale=socket.socket(socket.AF_UNIX,socket.SOCK_DGRAM);stale.bind(str(path));stale.close()
            with wakeup.Wakeup('enrichment') as receiver:
                assert receiver.socket is not None
            assert not path.exists()
            path.write_text('preserve')
            with wakeup.Wakeup('enrichment') as receiver:
                assert receiver.socket is None
                assert path.read_text() == 'preserve'
            # Rollback: no wakeup can be sent before the failed transaction exits.
            with patch.object(wakeup,'notify') as notify:
                try:
                    db.finish_enrichment_batch([{'job_id':row['id'],'result':result,'input_hash':'rollback'},
                        {'job_id':row['id'],'result':{},'input_hash':'invalid'}], 'test',client.version)
                except KeyError:pass
                else:raise AssertionError('Invalid transaction unexpectedly committed')
                assert not notify.called
            assert db.cached_enrichment('rollback') is None
            client.close()
            print('PASS stale-socket restart, protected non-socket path and no notification on rollback')


if __name__ == '__main__':
    main()
