"""Naive-UTC time helpers.

The whole codebase stores and compares *naive* UTC datetimes (see CLAUDE.md:
mixing tz-aware values in would break the market-data cache-age arithmetic).
``datetime.utcnow()`` and ``datetime.utcfromtimestamp()`` do exactly that but
are deprecated from Python 3.12, so these two helpers produce the same naive
UTC values via the non-deprecated tz-aware call plus an explicit strip.
"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """Current UTC time as a naive datetime (no tzinfo)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def utcfromtimestamp(ts: float) -> datetime:
    """Convert a POSIX timestamp to a naive UTC datetime."""
    return datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None)


def get_eastern_time(dt: datetime | None = None) -> datetime:
    """Return the current or provided naive UTC datetime converted to US Eastern time (naive)."""
    try:
        from zoneinfo import ZoneInfo
        eastern = ZoneInfo("America/New_York")
    except Exception:
        import pytz  # type: ignore[import-not-found]
        eastern = pytz.timezone("America/New_York")

    if dt is None:
        utc_dt = datetime.now(timezone.utc)
    elif dt.tzinfo is None:
        utc_dt = dt.replace(tzinfo=timezone.utc)
    else:
        utc_dt = dt.astimezone(timezone.utc)

    return utc_dt.astimezone(eastern).replace(tzinfo=None)


def get_market_session(dt: datetime | None = None) -> str:
    """Classify the trading session for US equity markets.

    Returns:
        - "REGULAR": 9:30 AM – 4:00 PM ET (Mon–Fri)
        - "EXTENDED_AFTER_HOURS": 4:00 PM – 8:00 PM ET (Mon–Fri)
        - "EXTENDED_PRE_MARKET": 4:00 AM – 9:30 AM ET (Mon–Fri)
        - "CLOSED": Weekends, and overnight (8:00 PM – 4:00 AM ET)
    """
    et = get_eastern_time(dt)
    if et.weekday() >= 5:  # Saturday or Sunday
        return "CLOSED"

    t = (et.hour, et.minute)
    if (9, 30) <= t < (16, 0):
        return "REGULAR"
    elif (16, 0) <= t < (20, 0):
        return "EXTENDED_AFTER_HOURS"
    elif (4, 0) <= t < (9, 30):
        return "EXTENDED_PRE_MARKET"
    else:
        return "CLOSED"


def is_extended_hours(dt: datetime | None = None) -> bool:
    """Return True if the current or specified time falls within US extended trading hours."""
    session = get_market_session(dt)
    return session in ("EXTENDED_AFTER_HOURS", "EXTENDED_PRE_MARKET")


def is_pentagon_drop_window(dt: datetime | None = None) -> bool:
    """Return True if within the 4:55 PM – 5:30 PM ET window when DoD contract announcements drop."""
    et = get_eastern_time(dt)
    if et.weekday() >= 5:
        return False
    t = (et.hour, et.minute)
    return (16, 55) <= t <= (17, 30)

