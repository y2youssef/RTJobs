"""Latency instrumentation: one row + one log line per measured stage.

Every measurement lands in the `latency_events` table (aggregated by
scripts/report_latency.py) and in the logs as a parseable INFO line:

    LATENCY source=linkedin stage=search_page seconds=12.403 detail={"new": 3}

Recording is best-effort: a metrics failure must never break scraping,
classification or delivery, so DB errors are swallowed (logged at DEBUG).
DB timestamps are local time strings; the seconds column is monotonic.
"""

import contextlib
import functools
import inspect
import json
import logging
import time

logger = logging.getLogger(__name__)


def record(source: str, stage: str, seconds: float, detail: dict | None = None):
    """Persist one measurement immediately; never raise into the caller."""
    try:
        from core import db
        db.record_latency(source, stage, seconds, detail)
    except Exception:
        # Metrics must never take the pipeline down (e.g. DB still starting).
        logger.debug("Latency recording failed for %s/%s", source, stage, exc_info=True)
    try:
        logger.info("LATENCY source=%s stage=%s seconds=%.3f%s", source, stage, seconds,
                    " detail=" + json.dumps(detail, ensure_ascii=False) if detail else "")
    except Exception:
        pass


@contextlib.contextmanager
def stage(source: str, name: str, detail=None):
    """Measure one stage. `detail` may be a dict or a callable returning one
    (callables see values computed after the measured body finished)."""
    start = time.monotonic()
    try:
        yield
    finally:
        payload = None
        try:
            payload = detail() if callable(detail) else detail
        except Exception:
            # A detail callable may reference names left unbound by the very
            # exception currently unwinding; metrics must never mask it.
            payload = None
        record(source, name, time.monotonic() - start, payload)


def timed(source: str, name: str, detail_fn=None):
    """Decorator variant of stage() for whole functions (sync and async).

    `detail_fn` is called with the same args as the wrapped function once it
    finished, so e.g. `lambda spider, page: {"new_jobs": len(spider._page_jobs)}`.
    Evaluation happens at call time; decoration itself never touches the DB.
    """
    def decorate(fn):
        def _payload(args, kwargs):
            if detail_fn is None:
                return None
            try:
                return detail_fn(*args, **kwargs)
            except Exception:
                return None

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args, **kwargs):
                start = time.monotonic()
                try:
                    return await fn(*args, **kwargs)
                finally:
                    record(source, name, time.monotonic() - start, _payload(args, kwargs))
            return async_wrapper

        @functools.wraps(fn)
        def sync_wrapper(*args, **kwargs):
            start = time.monotonic()
            try:
                return fn(*args, **kwargs)
            finally:
                record(source, name, time.monotonic() - start, _payload(args, kwargs))
        return sync_wrapper
    return decorate
