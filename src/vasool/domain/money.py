"""Money as an integer number of paise — the only currency representation in this codebase."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Money:
    paise: int

    def __post_init__(self) -> None:
        if isinstance(self.paise, bool) or not isinstance(self.paise, int):
            raise TypeError(f"Money.paise must be int, got {type(self.paise).__name__}")

    @classmethod
    def from_rupees(cls, rupees: float | str) -> Money:
        return cls(round(float(rupees) * 100))

    def to_rupees(self) -> float:
        return self.paise / 100

    def format_inr(self) -> str:
        rupees = abs(self.paise) / 100
        sign = "-" if self.paise < 0 else ""
        return f"{sign}₹{rupees:,.2f}"

    def __add__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paise + other.paise)

    def __sub__(self, other: object) -> Money:
        if not isinstance(other, Money):
            return NotImplemented
        return Money(self.paise - other.paise)

    def __mul__(self, factor: object) -> Money:
        if isinstance(factor, bool) or not isinstance(factor, int):
            return NotImplemented
        return Money(self.paise * factor)

    __rmul__ = __mul__
