"""Executor Protocol. RazorpayClient and SimulatorClient implement this identically and the
policy/harness layer can never tell them apart — CLAUDE.md invariant 5. Never import
`razorpay` outside execute/razorpay_client.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from vasool.domain.types import Attempt, FailureEvent, Invoice


@dataclass(frozen=True)
class AttemptOutcome:
    success: bool
    failure_event: FailureEvent | None


class Executor(Protocol):
    def execute(self, invoice: Invoice, attempt: Attempt, t: datetime, idempotency_key: str) -> AttemptOutcome: ...
