"""Deliver saved classifications independently of OpenRouter latency."""

import argparse
import fcntl
import logging
from pathlib import Path
import signal
import threading

from config import DB_PATH, ENRICHMENT_ENABLED, CLASSIFIED_DELIVERY_ENABLED, DELIVERY_POLL_SECONDS
from core import db, telegram
from core.classify import load_channels
from core.log import setup_logging
from core.wakeup import Wakeup

logger = logging.getLogger(__name__)


def deliver_once(stop=None):
    db.touch_worker('delivery', 'delivering')
    sent = telegram.notify_jobs(db.get_unnotified(), stop=stop,
                               progress=lambda: db.touch_worker('delivery', 'delivering'))
    db.touch_worker('delivery', 'idle')
    return sent


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
