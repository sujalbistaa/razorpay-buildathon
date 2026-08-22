"""Per-issuer token bucket. Pure and clock-injected: every method takes `at`, none reads a clock."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime


@dataclass(frozen=True)
class TokenBucket:
    capacity: float
    refill_per_hour: float
    tokens: float
    updated_at: datetime

    def tokens_at(self, at: datetime) -> float:
        elapsed_hours = (at - self.updated_at).total_seconds() / 3600
        if elapsed_hours < 0:
            raise ValueError("TokenBucket cannot be evaluated before its last update")
        return min(self.capacity, self.tokens + elapsed_hours * self.refill_per_hour)

    def consume(self, at: datetime, amount: float = 1.0) -> TokenBucket:
        available = self.tokens_at(at)
        if available < amount:
            raise ValueError("not enough tokens to consume")
        return replace(self, tokens=available - amount, updated_at=at)
