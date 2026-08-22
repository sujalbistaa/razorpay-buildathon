"""SimulatorClient — the Executor implementation that runs against the causal generative
World instead of real Razorpay test-mode APIs (CLAUDE.md invariant 5: the policy/harness
layer cannot tell this apart from RazorpayClient).

Caches by idempotency_key so a repeated execute() call for the same key returns the same
result rather than re-querying — invariant 6: "retries reuse the same key," and a caller
that retries must actually get idempotent behavior, not just a matching string.
"""

from __future__ import annotations

from datetime import datetime

from vasool.domain.types import Attempt, Invoice
from vasool.execute.protocol import AttemptOutcome
from vasool.sim.world import World


class SimulatorClient:
    def __init__(self, world: World) -> None:
        self._world = world
        self._seen: dict[str, AttemptOutcome] = {}

    def execute(self, invoice: Invoice, attempt: Attempt, t: datetime, idempotency_key: str) -> AttemptOutcome:
        cached = self._seen.get(idempotency_key)
        if cached is not None:
            return cached
        outcome = self._world.attempt(invoice, attempt, t)
        self._seen[idempotency_key] = outcome
        return outcome
