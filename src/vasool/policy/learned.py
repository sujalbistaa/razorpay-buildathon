"""LearnedPolicy — assembles hazard.py + planner.py + explore.py into BUILD_DOC.md §4.3's
differentiator, then falls back whole to HeuristicPolicy on a missing or corrupt model
artefact (CLAUDE.md: "Model file missing -> HeuristicPolicy," with a loud warning and a
`degraded` flag).

Only the FailureClass branches HeuristicPolicy routes to a *timed* silent-retry sequence
(SILENT_RETRY_AT_PAYDAY / _ON_DOWNTIME_RESOLVED / _NEXT_DAY / RETRY_T3_THEN_CONTACT) are
where a hazard model has anything to say -- credential-update, stop-never-retry and
contact-immediately are single fixed actions with no timing decision to optimise, so
LearnedPolicy defers to HeuristicPolicy's exact same branch for those rather than
reinventing them.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import OutboundMessage, RuleContext
from vasool.domain.types import ActionType, Attempt, Invoice, RecoveryPlan
from vasool.logging import get_logger
from vasool.policy.base import PolicyContext
from vasool.policy.downtime import DowntimeTracker
from vasool.policy.explore import ThompsonExplorer
from vasool.policy.hazard import HazardModel
from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction, HeuristicPolicy
from vasool.policy.payday import PaydayPosterior
from vasool.policy.planner import PlanningCosts, plan_ev_sequence
from vasool.policy.timing import avoid_quiet_hours

logger = get_logger(__name__)

POLICY_VERSION = "learned:v1"

# The only branches with a timing decision worth optimising -- see module docstring.
_EV_ELIGIBLE_ACTIONS: frozenset[HeuristicAction] = frozenset(
    {
        HeuristicAction.SILENT_RETRY_AT_PAYDAY,
        HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED,
        HeuristicAction.SILENT_RETRY_NEXT_DAY,
        HeuristicAction.RETRY_T3_THEN_CONTACT,
    }
)


def _stable_rng(invoice_id: str) -> np.random.Generator:
    # Thompson sampling needs a seeded generator per invariant 8 (no unseeded random); a
    # shared mutable Generator across invoices would make one invoice's plan depend on what
    # order other invoices were planned in, which breaks the per-arm determinism guarantee
    # the same way a shared World RNG would (sim/world.py's own module docstring).
    digest = hashlib.sha256(f"learned-explore:{invoice_id}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


class LearnedPolicy:
    def __init__(
        self, hazard: HazardModel | None, costs: PlanningCosts, explorer: ThompsonExplorer | None = None
    ) -> None:
        self._hazard = hazard
        self._costs = costs
        self._explorer = explorer
        self._fallback = HeuristicPolicy()
        self.degraded = hazard is None
        if self.degraded:
            logger.warning("learned_policy_degraded", reason="missing_or_corrupt_hazard_model")

    @classmethod
    def from_model_path(
        cls, model_path: Path, world_config: Mapping[str, Any], explorer: ThompsonExplorer | None = None
    ) -> LearnedPolicy:
        hazard = HazardModel.load(model_path)
        costs = PlanningCosts.from_world_config(world_config)
        return cls(hazard, costs, explorer)

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        if self._hazard is None:
            return self._fallback.plan(invoice, context)
        action = ACTION_TABLE[context.failure_class]
        if action not in _EV_ELIGIBLE_ACTIONS:
            return self._fallback.plan(invoice, context)
        return self._plan_ev(invoice, context)

    def _plan_ev(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        assert self._hazard is not None
        tracker = DowntimeTracker(context.known_downtime_windows)
        payday_estimate = PaydayPosterior.infer(context.payday_evidence).map_estimate()
        rng = _stable_rng(invoice.invoice_id) if self._explorer is not None else None

        result = plan_ev_sequence(
            invoice, context, self._hazard, self._costs, tracker, payday_estimate, self._explorer, rng
        )
        attempts = list(result.attempts)

        dunning = self._dunning_message(invoice, context, tuple(attempts))
        if dunning is not None:
            attempts.append(dunning)

        return RecoveryPlan(invoice_id=invoice.invoice_id, attempts=tuple(attempts), stop_rule=result.stop_rule)

    def _dunning_message(
        self, invoice: Invoice, context: PolicyContext, prior_attempts: tuple[Attempt, ...]
    ) -> Attempt | None:
        """The "+dunning" ablation stage (BUILD_DOC.md §8): one trailing CONTACT_LINK after
        the EV-planned silent retries, letting the customer complete payment manually if the
        automated sequence didn't recover it. Built the same way heuristic.py's own trailing
        contact is (a message, so it goes through R009/R010/R014 like any other), and simply
        dropped -- never forced -- when the invoice's message budget is already spent.
        """
        scheduled_times: list[datetime] = []
        for a in prior_attempts:
            t = a.debit_at or a.notify_at
            assert t is not None
            scheduled_times.append(t)
        last_time = max(scheduled_times, default=invoice.first_failed_at)
        send_at = avoid_quiet_hours(last_time + timedelta(hours=48))
        candidate = Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=len(prior_attempts),
            action_type=ActionType.CONTACT_LINK,
            rail=context.customer.mandate_rail,
            amount=invoice.amount,
            notify_at=send_at,
            debit_at=None,
        )
        message = OutboundMessage(
            merchant_name="Vasool", amount=invoice.amount, debit_date=send_at.date(), opt_out_included=True
        )
        rule_context = RuleContext(
            invoice=invoice, customer=context.customer, failure_class=context.failure_class, now=send_at,
            prior_attempts=prior_attempts, message=message,
        )
        verdict = ComplianceGuard.evaluate(
            RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(candidate,)), rule_context
        )
        return candidate if verdict.approved else None
