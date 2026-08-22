from datetime import UTC, date, datetime

import pytest

from vasool.domain.timezones import day_of_month_ist, ist_date, to_ist


def test_to_ist_converts_utc_to_ist() -> None:
    # 2026-08-22 18:35 UTC -> 2026-08-23 00:05 IST (UTC+5:30)
    utc_dt = datetime(2026, 8, 22, 18, 35, tzinfo=UTC)
    ist_dt = to_ist(utc_dt)
    assert (ist_dt.hour, ist_dt.minute) == (0, 5)
    assert ist_dt.date() == date(2026, 8, 23)


def test_to_ist_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        to_ist(datetime(2026, 8, 22, 18, 35))  # noqa: DTZ001 — naive on purpose


def test_ist_date_crosses_midnight() -> None:
    utc_dt = datetime(2026, 8, 22, 19, 0, tzinfo=UTC)
    assert ist_date(utc_dt) == date(2026, 8, 23)


def test_day_of_month_ist() -> None:
    utc_dt = datetime(2026, 8, 31, 19, 0, tzinfo=UTC)
    assert day_of_month_ist(utc_dt) == 1
