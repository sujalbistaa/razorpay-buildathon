"""LearnedPolicy: degrades cleanly to HeuristicPolicy without a model, defers to it exactly
for the branches with no timing decision to make, and every attempt it does plan clears
ComplianceGuard -- the same full-coverage discipline test_heuristic.py holds HeuristicPolicy
to.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import MESSAGE_ACTION_TYPES, OutboundMessage, RuleContext
from vasool.domain.money import Money
from vasool.domain.types import (
    ActionType,
    CustomerProfile,
    FailureClass,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
    RecoveryPlan,
)
from vasool.policy.base import PolicyContext
from vasool.policy.hazard import HazardExample, HazardFeatures, HazardModel
from vasool.policy.heuristic import HeuristicPolicy
from vasool.policy.learned import LearnedPolicy
from vasool.policy.planner import PlanningCosts

NOW = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)
COSTS = PlanningCosts(notification_cost=Money(20), annoyance_cost=Money(500))


def _invoice(**overrides: Any) -> Invoice:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1", "customer_id": "cust_1", "amount": Money.from_rupees(500),
        "category": InvoiceCategory.STANDARD, "first_failed_at": NOW,
    }
    defaults.update(overrides)
    return Invoice(**defaults)


def _customer(**overrides: Any) -> CustomerProfile:
    defaults: dict[str, Any] = {
        "customer_id": "cust_1", "split": "A", "language": "en", "mandate_rail": Rail.UPI_AUTOPAY,
        "mandate_state": MandateState.ACTIVE, "mandate_max_amount": Money.from_rupees(50_000), "issuer": "HDFC",
    }
    defaults.update(overrides)
    return CustomerProfile(**defaults)


def _context(**overrides: Any) -> PolicyContext:
    defaults: dict[str, Any] = {"customer": _customer(), "failure_class": FailureClass.INSUFFICIENT_FUNDS, "now": NOW}
    defaults.update(overrides)
    return PolicyContext(**defaults)


def _trained_model(seed: int = 1) -> HazardModel:
    rng = random.Random(seed)
    examples = [
        HazardExample(
            HazardFeatures(
                FailureClass.INSUFFICIENT_FUNDS, rng.uniform(0, 14), rng.uniform(-10, 10), True,
                rng.randint(0, 3), Money.from_rupees(rng.uniform(100, 2000)), Rail.UPI_AUTOPAY, rng.randint(0, 23),
            ),
            rng.random() < 0.6,
        )
        for _ in range(400)
    ]
    return HazardModel.train(examples)


def _message_for(attempt: Any) -> OutboundMessage | None:
    if attempt.action_type not in MESSAGE_ACTION_TYPES:
        return None
    send_at = attempt.notify_at or attempt.debit_at
    return OutboundMessage(merchant_name="Vasool", amount=attempt.amount, debit_date=send_at.date(), opt_out_included=True)


def _assert_plan_fully_compliant(invoice: Invoice, context: PolicyContext, plan: RecoveryPlan) -> None:
    notice_delivered_at = None
    prior: list[Any] = []
    for attempt in sorted(plan.attempts, key=lambda a: a.debit_at or a.notify_at):
        t = attempt.debit_at or attempt.notify_at
        rule_context = RuleContext(
            invoice=invoice, customer=context.customer, failure_class=context.failure_class, now=t,
            prior_attempts=tuple(prior), notice_delivered_at=notice_delivered_at, message=_message_for(attempt),
        )
        verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,)), rule_context)
        assert verdict.approved, f"{context.failure_class}: {attempt.action_type} rejected -- {[r for r in verdict.results if not r.passed]}"
        prior.append(attempt)
        if attempt.action_type is ActionType.PRE_DEBIT_NOTICE:
            notice_delivered_at = attempt.notify_at


def test_degraded_when_no_model_is_supplied() -> None:
    policy = LearnedPolicy(hazard=None, costs=COSTS)
    assert policy.degraded is True


def test_not_degraded_with_a_trained_model() -> None:
    policy = LearnedPolicy(hazard=_trained_model(), costs=COSTS)
    assert policy.degraded is False


def test_degraded_policy_matches_heuristic_exactly() -> None:
    invoice, context = _invoice(), _context()
    learned = LearnedPolicy(hazard=None, costs=COSTS).plan(invoice, context)
    heuristic = HeuristicPolicy().plan(invoice, context)
    assert learned == heuristic


@pytest.mark.parametrize(
    "failure_class",
    [FailureClass.CARD_EXPIRED, FailureClass.MANDATE_REVOKED, FailureClass.PAYMENT_RISK_CHECK_FAILED, FailureClass.AUTHENTICATION_FAILED],
)
def test_non_ev_eligible_classes_defer_to_heuristic_exactly(failure_class: FailureClass) -> None:
    invoice = _invoice()
    context = _context(failure_class=failure_class)
    learned = LearnedPolicy(hazard=_trained_model(), costs=COSTS).plan(invoice, context)
    heuristic = HeuristicPolicy().plan(invoice, context)
    assert learned == heuristic


@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_every_class_produces_a_compliance_passing_plan(failure_class: FailureClass) -> None:
    invoice = _invoice()
    context = _context(failure_class=failure_class)
    plan = LearnedPolicy(hazard=_trained_model(), costs=COSTS).plan(invoice, context)
    _assert_plan_fully_compliant(invoice, context, plan)


def test_ev_eligible_class_plan_may_include_a_trailing_dunning_message() -> None:
    invoice = _invoice()
    context = _context(failure_class=FailureClass.INSUFFICIENT_FUNDS)
    plan = LearnedPolicy(hazard=_trained_model(), costs=COSTS).plan(invoice, context)
    contact_links = [a for a in plan.attempts if a.action_type is ActionType.CONTACT_LINK]
    assert len(contact_links) <= 1
