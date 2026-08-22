"""Policy Protocol — pure function from (invoice, context) to a bounded RecoveryPlan.

No I/O, no clock reads, no DB access: everything a policy needs is in `context`. This is
what makes policy code table-testable and what makes the benchmark fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vasool.domain.types import Attempt, CustomerProfile, FailureClass, Invoice, RecoveryPlan


@dataclass(frozen=True)
class PolicyContext:
    customer: CustomerProfile
    failure_class: FailureClass
    now: datetime
    prior_attempts: tuple[Attempt, ...] = ()


class Policy(Protocol):
    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan: ...
