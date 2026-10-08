"""One clock for stored timestamps: naive UTC strings 'YYYY-MM-DD HH:MM:SS'.

Local wall time (TZ=Africa/Cairo) observes DST: every October an hour of
local timestamps repeats and every April one is skipped, which breaks retry
deadlines, queue ages and orderings computed from local strings. Everything
the pipeline schedules, compares or measures is therefore stored in UTC.

Only `posted_at` stays local wall time: it is a displayed property of the job
(Telegram "Posted ...") and analytics bucket it by local hour of day.
Convert with `to_local` whenever a stored timestamp is shown to a person.
"""

from datetime import date, datetime, timedelta, timezone
import time

FORMAT = "%Y-%m-%d %H:%M:%S"


def utcnow() -> datetime:
    """Naive UTC now, directly comparable with parse() of stored values."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def now_str() -> str:
    return utcnow().strftime(FORMAT)


def after(seconds: float) -> str:
    """Stored timestamp `seconds` from now (retry and alert deadlines)."""
    return (utcnow() + timedelta(seconds=seconds)).strftime(FORMAT)


def age_seconds(value: str | None, now: datetime | None = None) -> float | None:
    """Seconds since a stored timestamp, or None when missing/unparseable."""
    if not value:
        return None
    try:
        return ((now or utcnow()) - datetime.fromisoformat(value)).total_seconds()
    except ValueError:
        return None


def to_local(value: str | None) -> str:
    """Render a stored UTC timestamp in local wall time for messages."""
    if not value:
        return value or ""
    try:
        moment = datetime.fromisoformat(value).replace(tzinfo=timezone.utc)
    except ValueError:
        return value
    return moment.astimezone().strftime(FORMAT)


def next_local_midnight(today: date | None = None) -> str:
    """Stored (UTC) timestamp of the next local midnight — the budget-day boundary.

    mktime applies the local DST rules of THAT night, so the boundary is
    right even on the night the clocks change.
    """
    tomorrow = (today or datetime.now().date()) + timedelta(days=1)
    epoch = time.mktime((tomorrow.year, tomorrow.month, tomorrow.day, 0, 0, 0, 0, 0, -1))
    return datetime.fromtimestamp(epoch, timezone.utc).strftime(FORMAT)
