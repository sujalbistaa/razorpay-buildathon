import pytest

from vasool.domain.money import Money


def test_from_rupees_round_trips_to_paise() -> None:
    assert Money.from_rupees(1234.56).paise == 123456


def test_format_inr() -> None:
    assert Money.from_rupees(1234.56).format_inr() == "₹1,234.56"


def test_format_inr_negative() -> None:
    assert Money(-500).format_inr() == "-₹5.00"


def test_add() -> None:
    assert Money(100) + Money(50) == Money(150)


def test_sub() -> None:
    assert Money(100) - Money(50) == Money(50)


def test_mul_by_int() -> None:
    assert Money(100) * 3 == Money(300)
    assert 3 * Money(100) == Money(300)


def test_construction_rejects_float() -> None:
    with pytest.raises(TypeError):
        Money(1234.56)  # type: ignore[arg-type]


def test_construction_rejects_bool() -> None:
    with pytest.raises(TypeError):
        Money(True)  # type: ignore[arg-type]


def test_add_float_raises() -> None:
    with pytest.raises(TypeError):
        Money(100) + 5.0  # type: ignore[operator]


def test_mul_by_float_raises() -> None:
    with pytest.raises(TypeError):
        Money(100) * 1.5  # type: ignore[operator]
