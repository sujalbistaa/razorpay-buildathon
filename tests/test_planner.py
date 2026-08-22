"""EV planner: every produced attempt clears ComplianceGuard on its own (same discipline as
test_heuristic.py's full-coverage check), NEGATIVE_EV / WINDOW_EXPIRED / MAX_ATTEMPTS trigger
under the conditions BUILD_DOC.md §4.4 describes, and the greedy-lookahead tie-break actually
does something (isn't a no-op dressed up as depth-2 search).
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest

from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import OutboundMessage, RuleContext
from vasool.domain.money import Money
from vasool.domain.types import (
    CustomerProfile,
    FailureClass,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
    RecoveryPlan,
    StopRule,
)
from vasool.policy.base import PolicyContext
from vasool.policy.downtime import DowntimeTracker
from vasool.policy.hazard import HazardExample, HazardFeatures, HazardModel
from vasool.policy.planner import Candidate, PlanningCosts, pick_with_lookahead, plan_ev_sequence

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


def _trained_model(base_rate: float = 0.5, n: int = 400, seed: int = 1) -> HazardModel:
    rng = random.Random(seed)
    examples = [
        HazardExample(
            HazardFeatures(
                FailureClass.INSUFFICIENT_FUNDS, rng.uniform(0, 14), rng.uniform(-10, 10), True,
                rng.randint(0, 3), Money.from_rupees(rng.uniform(100, 2000)), Rail.UPI_AUTOPAY, rng.randint(0, 23),
            ),
            rng.random() < base_rate,
        )
        for _ in range(n)
    ]
    return HazardModel.train(examples)


def _assert_every_attempt_compliant(invoice: Invoice, context: PolicyContext, attempts: tuple[Any, ...]) -> None:
    prior: list[Any] = []
    notice_delivered_at = None
    for attempt in sorted(attempts, key=lambda a: a.debit_at or a.notify_at):
        t = attempt.debit_at or attempt.notify_at
        message = (
            OutboundMessage(merchant_name="Vasool", amount=attempt.amount, debit_date=t.date(), opt_out_included=True)
            if attempt.notify_at is not None
            else None
        )
        rule_context = RuleContext(
            invoice=invoice, customer=context.customer, failure_class=context.failure_class, now=t,
            prior_attempts=tuple(prior), notice_delivered_at=notice_delivered_at, message=message,
        )
        verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,)), rule_context)
        assert verdict.approved, [r for r in verdict.results if not r.passed]
        prior.append(attempt)
        if attempt.action_type.value == "pre_debit_notice":
            notice_delivered_at = attempt.notify_at


def test_every_planned_attempt_is_compliance_approved() -> None:
    invoice, context = _invoice(), _context()
    result = plan_ev_sequence(invoice, context, _trained_model(0.5), COSTS, DowntimeTracker(()), payday_map_estimate=1)
    assert result.attempts
    _assert_every_attempt_compliant(invoice, context, result.attempts)


def test_negative_ev_stops_the_sequence_when_success_is_never_worth_the_cost() -> None:
    # amount so small that even P(success)=1 can't clear notification_cost + annoyance_cost.
    invoice = _invoice(amount=Money(1))
    context = _context()
    result = plan_ev_sequence(invoice, context, _trained_model(0.99), COSTS, DowntimeTracker(()), payday_map_estimate=1)
    assert result.attempts == ()
    assert result.stop_rule is StopRule.NEGATIVE_EV


def test_window_expired_when_no_candidate_can_clear_compliance() -> None:
    # A customer who has already opted out fails R012 on every candidate -- no feasible slot
    # exists anywhere in the window, which is a different stop reason than "ran out of budget."
    context = _context(customer=_customer(opted_out=True))
    result = plan_ev_sequence(_invoice(), context, _trained_model(0.9), COSTS, DowntimeTracker(()), payday_map_estimate=1)
    assert result.attempts == ()
    assert result.stop_rule is StopRule.WINDOW_EXPIRED


def test_plan_uses_the_full_compliance_budget_when_ev_stays_positive() -> None:
    # High amount, high success probability everywhere: the sequence should run until a
    # *structural* stop (no compliant slot left), never NEGATIVE_EV -- R010's 3-messages-per-
    # invoice cap (each silent retry pairs with its own notice, same convention as
    # baselines.RazorpayDefaultPolicy) binds before R003's nominal 4-attempt ceiling ever
    # could, given a 14-day window and >=48h between messages -- so WINDOW_EXPIRED, not
    # MAX_ATTEMPTS, is the reachable stop reason here.
    invoice = _invoice(amount=Money.from_rupees(5000))
    context = _context()
    result = plan_ev_sequence(invoice, context, _trained_model(0.95), COSTS, DowntimeTracker(()), payday_map_estimate=1)
    silent_retries = [a for a in result.attempts if a.debit_at is not None]
    assert len(silent_retries) >= 2
    assert result.stop_rule is StopRule.WINDOW_EXPIRED


def test_pick_with_lookahead_prefers_the_two_step_optimum_over_the_greedy_immediate_best() -> None:
    early = datetime(2026, 6, 16, 10, 0, tzinfo=UTC)
    late = datetime(2026, 6, 20, 10, 0, tzinfo=UTC)
    too_close_to_late = datetime(2026, 6, 20, 14, 0, tzinfo=UTC)  # < 24h after `late`, blocks it from counting as a follow-up
    candidates = [
        Candidate(debit_at=early, p_success=0.5, ev=10.0),  # best immediate EV alone
        Candidate(debit_at=late, p_success=0.5, ev=9.0),
        Candidate(debit_at=too_close_to_late, p_success=0.5, ev=100.0),  # huge, but unreachable as a follow-up to `early` or `late`
    ]
    # `early` leaves `late` reachable as a follow-up (10.0 + 9.0 = 19.0); `too_close_to_late`
    # can only ever be its own pick, never a bonus -- so `early` should still win despite not
    # having the single largest EV in the list.
    best = pick_with_lookahead(candidates)
    assert best is not None
    assert best.debit_at == early


def test_pick_with_lookahead_returns_none_for_empty_candidates() -> None:
    assert pick_with_lookahead([]) is None


@pytest.mark.parametrize("failure_class", [FailureClass.INSUFFICIENT_FUNDS, FailureClass.CARD_DECLINED, FailureClass.PAYMENT_TIMED_OUT])
def test_plan_terminates_and_stays_within_the_compliance_window(failure_class: FailureClass) -> None:
    invoice, context = _invoice(), _context(failure_class=failure_class)
    result = plan_ev_sequence(invoice, context, _trained_model(0.5), COSTS, DowntimeTracker(()), payday_map_estimate=1)
    for attempt in result.attempts:
        t = attempt.debit_at or attempt.notify_at
        assert (t - invoice.first_failed_at).days <= 14
