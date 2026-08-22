"""HeuristicPolicy: full FailureClass coverage, and every branch's plan actually clears
ComplianceGuard -- a decision table that "looks right" but produces a plan the guard rejects
is worthless, so this is checked directly rather than trusted.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import OutboundMessage, RuleContext
from vasool.domain.money import Money
from vasool.domain.types import (
    CustomerProfile,
    DowntimeWindow,
    FailureClass,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
    Severity,
)
from vasool.policy.base import PolicyContext
from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction, HeuristicPolicy
from vasool.policy.payday import PaydayObservation

NOW = datetime(2026, 6, 15, 10, 0, tzinfo=UTC)  # 15:30 IST, safely outside quiet hours


def _invoice(**overrides: Any) -> Invoice:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1",
        "customer_id": "cust_1",
        "amount": Money.from_rupees(500),
        "category": InvoiceCategory.STANDARD,
        "first_failed_at": NOW,
    }
    defaults.update(overrides)
    return Invoice(**defaults)


def _customer(**overrides: Any) -> CustomerProfile:
    defaults: dict[str, Any] = {
        "customer_id": "cust_1",
        "split": "A",
        "language": "en",
        "mandate_rail": Rail.UPI_AUTOPAY,
        "mandate_state": MandateState.ACTIVE,
        "mandate_max_amount": Money.from_rupees(50_000),
        "issuer": "HDFC",
        "opted_out": False,
        "promise_to_pay_until": None,
    }
    defaults.update(overrides)
    return CustomerProfile(**defaults)


def _context(**overrides: Any) -> PolicyContext:
    defaults: dict[str, Any] = {"customer": _customer(), "failure_class": FailureClass.INSUFFICIENT_FUNDS, "now": NOW}
    defaults.update(overrides)
    return PolicyContext(**defaults)


def test_action_table_covers_every_failure_class() -> None:
    assert set(ACTION_TABLE.keys()) == set(FailureClass)


def _message_for(attempt: Any) -> OutboundMessage | None:
    from vasool.compliance.rules import MESSAGE_ACTION_TYPES

    if attempt.action_type not in MESSAGE_ACTION_TYPES:
        return None
    send_at = attempt.notify_at or attempt.debit_at
    return OutboundMessage(merchant_name="Vasool", amount=attempt.amount, debit_date=send_at.date(), opt_out_included=True)


@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_every_class_produces_a_compliance_passing_plan(failure_class: FailureClass) -> None:
    from vasool.domain.types import ActionType, RecoveryPlan

    invoice = _invoice()
    context = _context(failure_class=failure_class)
    plan = HeuristicPolicy().plan(invoice, context)

    notice_delivered_at = None
    prior: list[Any] = []
    for attempt in sorted(plan.attempts, key=lambda a: a.debit_at or a.notify_at):
        t = attempt.debit_at or attempt.notify_at
        rule_context = RuleContext(
            invoice=invoice,
            customer=context.customer,
            failure_class=context.failure_class,
            now=t,
            prior_attempts=tuple(prior),
            notice_delivered_at=notice_delivered_at,
            message=_message_for(attempt),
        )
        verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,)), rule_context)
        assert verdict.approved, f"{failure_class}: {attempt.action_type} rejected -- {[r for r in verdict.results if not r.passed]}"
        prior.append(attempt)
        if attempt.action_type is ActionType.PRE_DEBIT_NOTICE:
            notice_delivered_at = attempt.notify_at


def test_insufficient_funds_with_no_evidence_uses_earliest_reachable_window_day() -> None:
    # No evidence -> population-prior MAP (day 1-3), whose next real occurrence is always
    # weeks out from a mid-month `now` -- unreachable within the 14-day compliance window,
    # so this should land on the earliest day the window allows (today+2), not some
    # unrelated fixed offset and not a wraparound toward the end of the month.
    context = _context(failure_class=FailureClass.INSUFFICIENT_FUNDS, payday_evidence=())
    plan = HeuristicPolicy().plan(_invoice(), context)
    silent_retry = next(a for a in plan.attempts if a.debit_at is not None)
    assert (silent_retry.debit_at.date() - NOW.date()).days == 2


def test_insufficient_funds_uses_inferred_payday_when_confident() -> None:
    # Demonstrably out of money on days 1-10 -- kills both of the prior's dominant early-
    # month clusters, so the posterior should land in the late-month cluster instead.
    evidence = tuple(
        PaydayObservation(day_of_month=d, insufficient_funds=True) for d in range(1, 11) for _ in range(3)
    )
    context = _context(failure_class=FailureClass.INSUFFICIENT_FUNDS, payday_evidence=evidence)
    plan = HeuristicPolicy().plan(_invoice(), context)
    silent_retry = next(a for a in plan.attempts if a.debit_at is not None)
    assert 1 <= (silent_retry.debit_at.date() - NOW.date()).days <= 14
    assert silent_retry.debit_at.day not in range(1, 11)


def test_early_month_target_never_wraps_to_the_discouraged_late_month_range() -> None:
    # Regression: a naive circular day-of-month distance treats day 1 as "close to" day 29,
    # which would schedule debits exactly in the 25th-31st range Razorpay's guidance says
    # to avoid. With no evidence (MAP lands in the 1-3 cluster) and a mid-month `now`, the
    # target's next occurrence is always unreachable within 14 days -- must fall to the
    # earliest window day, never wrap toward month-end.
    context = _context(failure_class=FailureClass.INSUFFICIENT_FUNDS, payday_evidence=())
    plan = HeuristicPolicy().plan(_invoice(), context)
    silent_retry = next(a for a in plan.attempts if a.debit_at is not None)
    assert silent_retry.debit_at.day not in range(25, 32)


def test_gateway_technical_error_uses_downtime_resolution_when_known() -> None:
    window = DowntimeWindow(
        issuer="HDFC", method=Rail.UPI_AUTOPAY, severity=Severity.HIGH,
        begin=NOW - timedelta(hours=1), end=NOW + timedelta(hours=3),
    )
    context = _context(failure_class=FailureClass.GATEWAY_TECHNICAL_ERROR, known_downtime_windows=(window,))
    plan = HeuristicPolicy().plan(_invoice(), context)
    silent_retry = next(a for a in plan.attempts if a.debit_at is not None)
    assert silent_retry.debit_at >= window.end


def test_gateway_technical_error_falls_back_when_no_known_window() -> None:
    context = _context(failure_class=FailureClass.GATEWAY_TECHNICAL_ERROR, known_downtime_windows=())
    plan = HeuristicPolicy().plan(_invoice(), context)
    silent_retry = next(a for a in plan.attempts if a.debit_at is not None)
    assert (silent_retry.debit_at.date() - NOW.date()).days == 1


def test_payment_risk_check_failed_never_retries() -> None:
    context = _context(failure_class=FailureClass.PAYMENT_RISK_CHECK_FAILED)
    plan = HeuristicPolicy().plan(_invoice(), context)
    assert plan.attempts == ()


def test_hard_declines_get_exactly_one_credential_update_attempt() -> None:
    for fc in (FailureClass.CARD_EXPIRED, FailureClass.DEBIT_INSTRUMENT_BLOCKED, FailureClass.MANDATE_REVOKED):
        context = _context(failure_class=fc)
        plan = HeuristicPolicy().plan(_invoice(), context)
        assert len(plan.attempts) == 1
        assert plan.attempts[0].action_type.value == "credential_update_request"


def test_unknown_retries_once_then_contacts() -> None:
    context = _context(failure_class=FailureClass.UNKNOWN)
    plan = HeuristicPolicy().plan(_invoice(), context)
    action_types = [a.action_type.value for a in sorted(plan.attempts, key=lambda a: a.debit_at or a.notify_at)]
    assert action_types == ["pre_debit_notice", "silent_retry", "contact_link"]


def test_action_covers_documented_examples_from_build_doc() -> None:
    # BUILD_DOC.md §4.2's 8 worked rows, spot-checked against the table directly.
    assert ACTION_TABLE[FailureClass.INSUFFICIENT_FUNDS] is HeuristicAction.SILENT_RETRY_AT_PAYDAY
    assert ACTION_TABLE[FailureClass.GATEWAY_TECHNICAL_ERROR] is HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED
    assert ACTION_TABLE[FailureClass.VELOCITY_EXCEEDED] is HeuristicAction.SILENT_RETRY_NEXT_DAY
    assert ACTION_TABLE[FailureClass.AUTHENTICATION_FAILED] is HeuristicAction.CONTACT_IMMEDIATELY
    assert ACTION_TABLE[FailureClass.CARD_EXPIRED] is HeuristicAction.CREDENTIAL_UPDATE_THEN_STOP
    assert ACTION_TABLE[FailureClass.DEBIT_INSTRUMENT_BLOCKED] is HeuristicAction.CREDENTIAL_UPDATE_THEN_STOP
    assert ACTION_TABLE[FailureClass.MANDATE_REVOKED] is HeuristicAction.CREDENTIAL_UPDATE_THEN_STOP
    assert ACTION_TABLE[FailureClass.PAYMENT_RISK_CHECK_FAILED] is HeuristicAction.STOP_NEVER_RETRY
    assert ACTION_TABLE[FailureClass.UNKNOWN] is HeuristicAction.RETRY_T3_THEN_CONTACT
