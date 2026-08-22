from datetime import UTC, datetime

import pytest

from vasool.domain.clock import FrozenClock, SystemClock


def test_system_clock_returns_tz_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == UTC.utcoffset(now)


def test_frozen_clock_returns_fixed_time() -> None:
    at = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    clock = FrozenClock(at)
    assert clock.now() == at
    assert clock.now() == at  # calling twice must not advance it


def test_frozen_clock_rejects_naive_datetime() -> None:
    with pytest.raises(ValueError):
        FrozenClock(datetime(2026, 8, 22, 12, 0))  # noqa: DTZ001 — naive on purpose
