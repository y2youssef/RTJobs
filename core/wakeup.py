"""Best-effort worker wakeups over the shared data volume.

SQLite owns the work. These datagrams carry no jobs and only tell a worker to
check its queue now. Call notify after commit; hold the worker's process lock
before binding. Startup scans and timeout scans recover missed notifications.
"""

import errno
import logging
from pathlib import Path
import select
import socket
import stat

from config import DB_PATH

logger = logging.getLogger(__name__)


def _path(worker: str) -> Path:
    if worker not in ('enrichment', 'delivery'):
        raise ValueError('Unknown queue worker')
    return Path(DB_PATH + '.' + worker + '.wake.sock')


def notify(worker: str) -> bool:
    """Never block the producer or turn a successful commit into a failure."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.setblocking(False)
            sender.sendto(b'wake', str(_path(worker)))
        return True
    except OSError as exc:
        # A stopped worker, a full socket queue or a restart is harmless: SQLite
        # retains the work. A full queue already contains a wakeup to consume.
        logger.debug('Worker %s wakeup unavailable: %s', worker, type(exc).__name__)
        return False


class Wakeup:
    """One receiver per locked worker; bind before its first SQLite scan."""

    def __init__(self, worker: str):
        self.worker = worker
        self.path = _path(worker)
        self.socket = None
        self.inode = None

    def __enter__(self):
        try:
            try:
                previous = self.path.lstat()
            except FileNotFoundError:
                previous = None
            if previous is not None:
                # Never delete a regular file or symlink found at this path.
                if not stat.S_ISSOCK(previous.st_mode):
                    raise OSError(errno.EEXIST, 'Wakeup path is not a socket')
                self.path.unlink()  # Stale socket after a killed worker.
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.socket.setblocking(False)
            self.socket.bind(str(self.path))
            self.inode = self.path.stat().st_ino
            self.path.chmod(0o600)
            logger.info('Immediate %s queue notifications enabled', self.worker)
        except OSError as exc:
            self.__exit__(None, None, None)
            logger.warning('Worker %s using recovery polling: %s', self.worker, type(exc).__name__)
        return self

    def clear(self):
        """Coalesce hints BEFORE scanning SQLite, never after an empty scan."""
        if self.socket is not None:
            for _ in range(256):
                try:
                    self.socket.recv(64)
                except BlockingIOError:
                    break

    def wait(self, stop, timeout: float):
        """Wait for a commit hint or a recovery/retry scan, whichever is first."""
        if stop.is_set():
            return
        if self.socket is None:
            stop.wait(timeout)
        elif select.select([self.socket], [], [], timeout)[0]:
            logger.info('Worker %s received queue notification', self.worker)

    def stop(self, event):
        """Wake select immediately on SIGTERM/SIGINT, as well as setting stop."""
        event.set()
        notify(self.worker)

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()
            self.socket = None
        try:
            if self.inode is not None and self.path.lstat().st_ino == self.inode:
                self.path.unlink()
        except FileNotFoundError:
            pass
