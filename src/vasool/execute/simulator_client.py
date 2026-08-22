"""SimulatorClient — the Executor implementation that runs against the causal generative
World instead of real Razorpay test-mode APIs (CLAUDE.md invariant 5: the policy/harness
layer cannot tell this apart from RazorpayClient).

Caches by idempotency_key so a repeated execute() call for the same key returns the same
result rather than re-querying — invariant 6: "retries reuse the same key," and a caller
that retries must actually get idempotent behavior, not just a matching string.
"""

from __future__ import annotations

from datetime import datetime

from vasool.domain.types import ActionType, Attempt, Invoice
from vasool.execute.protocol import AttemptOutcome
from vasool.sim.world import World


class SimulatorClient:
    def __init__(self, world: World) -> None:
        self._world = world
        self._seen: dict[str, AttemptOutcome] = {}
        # How many CONTACT_LINK attempts have already executed per invoice -- World.attempt()
        # needs this for fatigue decay but the Executor Protocol can't carry it (a real
        # RazorpayClient has no such parameter; a live system would derive the same count
        # from its own send log). Tracked here, not in World, so World.attempt() stays a pure
        # function of exactly the arguments it's given.
        self._message_counts: dict[str, int] = {}

    def execute(self, invoice: Invoice, attempt: Attempt, t: datetime, idempotency_key: str) -> AttemptOutcome:
        cached = self._seen.get(idempotency_key)
        if cached is not None:
            return cached
        prior_message_count = self._message_counts.get(invoice.invoice_id, 0)
        outcome = self._world.attempt(invoice, attempt, t, prior_message_count=prior_message_count)
        self._seen[idempotency_key] = outcome
        if attempt.action_type is ActionType.CONTACT_LINK:
            self._message_counts[invoice.invoice_id] = prior_message_count + 1
        return outcome
