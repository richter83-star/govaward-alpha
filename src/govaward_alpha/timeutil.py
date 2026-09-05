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
