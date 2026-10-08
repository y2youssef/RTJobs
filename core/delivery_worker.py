"""Deliver saved classifications independently of OpenRouter latency."""

import argparse
import fcntl
import logging
from datetime import datetime
from pathlib import Path
import signal
import threading
import time

from config import DB_PATH, ENRICHMENT_ENABLED, CLASSIFIED_DELIVERY_ENABLED, DELIVERY_POLL_SECONDS
from core import db, telegram, timing
from core.classify import load_channels
from core.log import setup_logging
from core.wakeup import Wakeup

logger = logging.getLogger(__name__)


def deliver_once(stop=None):
    # Idle scans (every DELIVERY_POLL_SECONDS) record nothing: latency rows
    # only for batches that actually carried messages, or the table fills
    # with thousands of sent=0 measurements per day.
    db.touch_worker('delivery', 'delivering')
    oldest = db.oldest_undelivered_ready_at()
    start = time.monotonic()
    sent = telegram.notify_jobs(db.get_unnotified(), stop=stop,
                                progress=lambda: db.touch_worker('delivery', 'delivering'))
    elapsed = time.monotonic() - start
    if sent:
        timing.record('delivery', 'delivery_batch', elapsed, {'sent': sent})
        _record_queue_delay(oldest)
    db.touch_worker('delivery', 'idle')
    return sent


def _record_queue_delay(oldest):
    """Time the head of the queue waited for Telegram since becoming ready."""
    if not oldest:
        return
    try:
        wait = (datetime.now() - datetime.fromisoformat(oldest)).total_seconds()
    except ValueError:
        return
    timing.record('delivery', 'queue_delay', max(0, wait), None)


def run_worker(stop, wakeup, once=False):
    """Deliver on publication; timeout scans recover missed hints and retries."""
    while not stop.is_set():
        wakeup.clear()
        sent = deliver_once(stop)
        if sent:
            logger.info('Delivered %s jobs', sent)
        if once:
            break
        if not sent:
            wakeup.wait(stop, DELIVERY_POLL_SECONDS)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    if not (ENRICHMENT_ENABLED and CLASSIFIED_DELIVERY_ENABLED):
        print('Classified delivery is disabled.')
        return 0
    setup_logging()
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with open(DB_PATH + '.delivery.lock', 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logger.info('Another delivery worker is active')
            return 0
        db.init_db()
        load_channels(require_complete=True)
        stop = threading.Event()
        try:
            with Wakeup('delivery') as wakeup:
                for sig in (signal.SIGTERM, signal.SIGINT):
                    signal.signal(sig, lambda *_: wakeup.stop(stop))
                run_worker(stop, wakeup, once=args.once)
        finally:
            db.touch_worker('delivery', 'stopped')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
