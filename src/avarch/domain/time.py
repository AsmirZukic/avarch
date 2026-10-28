"""Persistence timestamps are UTC; SQLite may return them without tzinfo."""

from datetime import UTC, datetime


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def datetime_after(left: datetime, right: datetime) -> bool:
    return as_utc(left) > as_utc(right)
