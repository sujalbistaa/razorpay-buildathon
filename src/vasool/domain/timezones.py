"""Store UTC, evaluate business rules in Asia/Kolkata — payday and quiet hours are IST concepts."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("to_ist requires a timezone-aware datetime")
    return dt.astimezone(IST)


def ist_date(dt: datetime) -> date:
    return to_ist(dt).date()


def day_of_month_ist(dt: datetime) -> int:
    return to_ist(dt).day


def at_hour_ist(d: date, hour: int) -> datetime:
    return datetime(d.year, d.month, d.day, hour, tzinfo=IST)
