"""Timestamp helpers.

PostgreSQL returns timezone-aware values for ``TIMESTAMPTZ`` columns, while
SQLite (used for fast tests) returns naive values. Expiry and revocation checks
must therefore normalize before comparing.
"""

from __future__ import annotations

import datetime as dt


def utc_now() -> dt.datetime:
    """Return the current time as an aware UTC value."""
    return dt.datetime.now(tz=dt.UTC)


def as_utc(value: dt.datetime | None) -> dt.datetime | None:
    """Interpret naive database values as UTC and normalize aware values."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def is_expired(value: dt.datetime | None, *, now: dt.datetime | None = None) -> bool:
    """Return ``True`` when an expiry timestamp is set and has passed."""
    normalized = as_utc(value)
    if normalized is None:
        return False
    return normalized <= (now or utc_now())
