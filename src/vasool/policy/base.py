"""Policy Protocol — pure function from (invoice, context) to a bounded RecoveryPlan.

No I/O, no clock reads, no DB access: everything a policy needs is in `context`. This is
what makes policy code table-testable and what makes the benchmark fast.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vasool.domain.types import (
    Attempt,
    CustomerProfile,
    DowntimeWindow,
    FailureClass,
    Invoice,
    RecoveryPlan,
)
from vasool.policy.payday import PaydayObservation


@dataclass(frozen=True)
class PolicyContext:
    customer: CustomerProfile
    failure_class: FailureClass
    now: datetime
    prior_attempts: tuple[Attempt, ...] = ()
    # Both empty by default and ignored by the baselines -- only HeuristicPolicy reads them.
    # Built by the harness from Cohort.origin_failures / World.downtime_windows_known_by(),
    # never from LatentCustomer directly (BUILD_DOC.md §3: policy never sees hidden state).
    payday_evidence: tuple[PaydayObservation, ...] = ()
    known_downtime_windows: tuple[DowntimeWindow, ...] = ()


class Policy(Protocol):
    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan: ...
