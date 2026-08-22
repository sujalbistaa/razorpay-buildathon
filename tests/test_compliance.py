"""Table-driven: every rule gets at least one passing, one failing, and one boundary case.

CASES is the single source of truth. test_every_rule_has_a_passing_and_failing_case and
test_every_rule_has_at_least_three_cases enforce the Phase 2 acceptance bar structurally,
so a rule added without enough coverage fails the suite rather than relying on review.
"""

from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from vasool.compliance.buckets import TokenBucket
from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import (
    ALL_RULES,
    R001_PRE_DEBIT_NOTICE,
    R002_AFA_THRESHOLD,
    R003_MAX_SILENT_ATTEMPTS,
    R004_ATTEMPT_WINDOW,
    R005_MIN_INTERVAL_SAME_PATH,
    R006_HARD_DECLINE_NO_RETRY,
    R007_MANDATE_ACTIVE,
    R008_MANDATE_CAP,
    R009_CONTACT_QUIET_HOURS,
    R010_MESSAGE_FREQUENCY,
    R011_ISSUER_RATE_LIMIT,
    R012_CUSTOMER_OPT_OUT,
    R013_PROMISE_TO_PAY_SUPPRESSION,
    R014_MESSAGE_CONTENT,
    R015_ISSUER_DOWNTIME_GATE,
    OutboundMessage,
    Rule,
    RuleContext,
)
from vasool.domain.money import Money
from vasool.domain.timezones import IST
from vasool.domain.types import (
    ActionType,
    Attempt,
    CustomerProfile,
    FailureClass,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def ist_dt(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 8, 22, hour, minute, second, tzinfo=IST)


def _invoice(**overrides: Any) -> Invoice:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1",
        "customer_id": "cust_1",
        "amount": Money.from_rupees(1000),
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


def _attempt(**overrides: Any) -> Attempt:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1",
        "attempt_index": 0,
        "action_type": ActionType.SILENT_RETRY,
        "rail": Rail.UPI_AUTOPAY,
        "amount": Money.from_rupees(1000),
        "notify_at": None,
        "debit_at": NOW,
    }
    defaults.update(overrides)
    return Attempt(**defaults)


def _context(**overrides: Any) -> RuleContext:
    defaults: dict[str, Any] = {
        "invoice": _invoice(),
        "customer": _customer(),
        "failure_class": FailureClass.INSUFFICIENT_FUNDS,
        "now": NOW,
    }
    defaults.update(overrides)
    return RuleContext(**defaults)


Case = tuple[str, Rule, dict[str, Any], dict[str, Any], bool]
CASES: list[Case] = []


def add_case(
    name: str,
    rule: Rule,
    *,
    attempt: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    passed: bool,
) -> None:
    CASES.append((name, rule, attempt or {}, context or {}, passed))


# R001_PRE_DEBIT_NOTICE
add_case("r001_no_notice", R001_PRE_DEBIT_NOTICE, context={"notice_delivered_at": None}, passed=False)
add_case(
    "r001_ample_notice",
    R001_PRE_DEBIT_NOTICE,
    context={"notice_delivered_at": NOW - timedelta(hours=48)},
    passed=True,
)
add_case(
    "r001_exactly_24h_boundary",
    R001_PRE_DEBIT_NOTICE,
    context={"notice_delivered_at": NOW - timedelta(hours=24)},
    passed=True,
)
add_case(
    "r001_just_under_24h_boundary",
    R001_PRE_DEBIT_NOTICE,
    context={"notice_delivered_at": NOW - timedelta(hours=24) + timedelta(seconds=1)},
    passed=False,
)
add_case(
    "r001_not_a_debit",
    R001_PRE_DEBIT_NOTICE,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={"notice_delivered_at": None},
    passed=True,
)

# R002_AFA_THRESHOLD
add_case(
    "r002_exactly_15000_standard",
    R002_AFA_THRESHOLD,
    attempt={"amount": Money.from_rupees(15_000)},
    passed=True,
)
add_case(
    "r002_exactly_15000_01_standard",
    R002_AFA_THRESHOLD,
    attempt={"amount": Money.from_rupees(15_000.01)},
    passed=False,
)
add_case(
    "r002_exactly_100000_elevated",
    R002_AFA_THRESHOLD,
    attempt={"amount": Money.from_rupees(1_00_000)},
    context={"invoice": _invoice(category=InvoiceCategory.INSURANCE)},
    passed=True,
)
add_case(
    "r002_exactly_100000_non_elevated",
    R002_AFA_THRESHOLD,
    attempt={"amount": Money.from_rupees(1_00_000)},
    passed=False,
)
add_case(
    "r002_above_limit_but_interactive",
    R002_AFA_THRESHOLD,
    attempt={"amount": Money.from_rupees(1_00_000), "action_type": ActionType.CONTACT_LINK},
    passed=True,
)

# R003_MAX_SILENT_ATTEMPTS
add_case("r003_first_attempt", R003_MAX_SILENT_ATTEMPTS, passed=True)
add_case(
    "r003_fourth_attempt_boundary",
    R003_MAX_SILENT_ATTEMPTS,
    context={
        "prior_attempts": tuple(
            _attempt(attempt_index=i, action_type=ActionType.SILENT_RETRY) for i in range(3)
        )
    },
    passed=True,
)
add_case(
    "r003_fifth_attempt_boundary",
    R003_MAX_SILENT_ATTEMPTS,
    context={
        "prior_attempts": tuple(
            _attempt(attempt_index=i, action_type=ActionType.SILENT_RETRY) for i in range(4)
        )
    },
    passed=False,
)
add_case(
    "r003_non_silent_action_exempt",
    R003_MAX_SILENT_ATTEMPTS,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={
        "prior_attempts": tuple(
            _attempt(attempt_index=i, action_type=ActionType.SILENT_RETRY) for i in range(10)
        )
    },
    passed=True,
)

# R004_ATTEMPT_WINDOW
add_case("r004_one_day_after", R004_ATTEMPT_WINDOW, attempt={"debit_at": NOW + timedelta(days=1)}, passed=True)
add_case(
    "r004_exactly_14_days_boundary",
    R004_ATTEMPT_WINDOW,
    attempt={"debit_at": NOW + timedelta(days=14)},
    passed=True,
)
add_case(
    "r004_14_days_plus_1s_boundary",
    R004_ATTEMPT_WINDOW,
    attempt={"debit_at": NOW + timedelta(days=14, seconds=1)},
    passed=False,
)
add_case(
    "r004_no_scheduled_time",
    R004_ATTEMPT_WINDOW,
    attempt={"action_type": ActionType.STOP, "debit_at": None, "notify_at": None},
    passed=True,
)

# R005_MIN_INTERVAL_SAME_PATH
add_case("r005_no_prior_attempts", R005_MIN_INTERVAL_SAME_PATH, passed=True)
add_case(
    "r005_exactly_24h_boundary",
    R005_MIN_INTERVAL_SAME_PATH,
    context={"prior_attempts": (_attempt(debit_at=NOW - timedelta(hours=24)),)},
    passed=True,
)
add_case(
    "r005_just_under_24h_boundary",
    R005_MIN_INTERVAL_SAME_PATH,
    context={
        "prior_attempts": (_attempt(debit_at=NOW - timedelta(hours=24) + timedelta(seconds=1)),)
    },
    passed=False,
)
add_case(
    "r005_different_rail_exempt",
    R005_MIN_INTERVAL_SAME_PATH,
    context={
        "prior_attempts": (_attempt(rail=Rail.CARD, debit_at=NOW - timedelta(minutes=1)),)
    },
    passed=True,
)

# R006_HARD_DECLINE_NO_RETRY
add_case(
    "r006_hard_decline_silent_retry",
    R006_HARD_DECLINE_NO_RETRY,
    context={"failure_class": FailureClass.CARD_EXPIRED},
    passed=False,
)
add_case(
    "r006_hard_decline_credential_update_ok",
    R006_HARD_DECLINE_NO_RETRY,
    attempt={"action_type": ActionType.CREDENTIAL_UPDATE_REQUEST, "debit_at": None, "notify_at": NOW},
    context={"failure_class": FailureClass.CARD_EXPIRED},
    passed=True,
)
add_case(
    "r006_soft_decline_silent_retry_ok",
    R006_HARD_DECLINE_NO_RETRY,
    context={"failure_class": FailureClass.INSUFFICIENT_FUNDS},
    passed=True,
)
add_case(
    "r006_hard_decline_pre_debit_notice_blocked",
    R006_HARD_DECLINE_NO_RETRY,
    attempt={"action_type": ActionType.PRE_DEBIT_NOTICE, "notify_at": NOW},
    context={"failure_class": FailureClass.PAYMENT_RISK_CHECK_FAILED},
    passed=False,
)

# R007_MANDATE_ACTIVE
add_case("r007_active_mandate", R007_MANDATE_ACTIVE, passed=True)
add_case(
    "r007_revoked_mandate",
    R007_MANDATE_ACTIVE,
    context={"customer": _customer(mandate_state=MandateState.REVOKED)},
    passed=False,
)
add_case(
    "r007_paused_mandate",
    R007_MANDATE_ACTIVE,
    context={"customer": _customer(mandate_state=MandateState.PAUSED)},
    passed=False,
)
add_case(
    "r007_non_debit_exempt",
    R007_MANDATE_ACTIVE,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={"customer": _customer(mandate_state=MandateState.REVOKED)},
    passed=True,
)

# R008_MANDATE_CAP
add_case(
    "r008_exactly_at_cap_boundary",
    R008_MANDATE_CAP,
    attempt={"amount": Money.from_rupees(50_000)},
    passed=True,
)
add_case(
    "r008_one_paise_over_cap_boundary",
    R008_MANDATE_CAP,
    attempt={"amount": Money(50_000 * 100 + 1)},
    passed=False,
)
add_case("r008_well_under_cap", R008_MANDATE_CAP, attempt={"amount": Money.from_rupees(100)}, passed=True)
add_case(
    "r008_non_debit_exempt",
    R008_MANDATE_CAP,
    attempt={"action_type": ActionType.CONTACT_LINK, "amount": Money.from_rupees(99_000), "debit_at": None, "notify_at": NOW},
    passed=True,
)

# R009_CONTACT_QUIET_HOURS
_msg_attempt = {"action_type": ActionType.CONTACT_LINK, "debit_at": None}
add_case(
    "r009_just_before_quiet_start",
    R009_CONTACT_QUIET_HOURS,
    attempt={**_msg_attempt, "notify_at": ist_dt(20, 59, 59)},
    passed=True,
)
add_case(
    "r009_exactly_21_00_00_ist_boundary",
    R009_CONTACT_QUIET_HOURS,
    attempt={**_msg_attempt, "notify_at": ist_dt(21, 0, 0)},
    passed=False,
)
add_case(
    "r009_08_59_59_ist_still_quiet",
    R009_CONTACT_QUIET_HOURS,
    attempt={**_msg_attempt, "notify_at": ist_dt(8, 59, 59)},
    passed=False,
)
add_case(
    "r009_exactly_09_00_00_ist_boundary",
    R009_CONTACT_QUIET_HOURS,
    attempt={**_msg_attempt, "notify_at": ist_dt(9, 0, 0)},
    passed=True,
)
add_case(
    "r009_silent_retry_exempt",
    R009_CONTACT_QUIET_HOURS,
    attempt={"debit_at": ist_dt(21, 0, 0)},
    passed=True,
)

# R010_MESSAGE_FREQUENCY
add_case("r010_first_message", R010_MESSAGE_FREQUENCY, attempt=_msg_attempt | {"notify_at": NOW}, passed=True)
add_case(
    "r010_third_message_boundary",
    R010_MESSAGE_FREQUENCY,
    attempt=_msg_attempt | {"notify_at": NOW},
    context={
        "prior_attempts": tuple(
            _attempt(
                attempt_index=i,
                action_type=ActionType.CONTACT_LINK,
                debit_at=None,
                notify_at=NOW - timedelta(hours=48 * (2 - i)),
            )
            for i in range(2)
        )
    },
    passed=True,
)
add_case(
    "r010_fourth_message_boundary",
    R010_MESSAGE_FREQUENCY,
    attempt=_msg_attempt | {"notify_at": NOW},
    context={
        "prior_attempts": tuple(
            _attempt(
                attempt_index=i,
                action_type=ActionType.CONTACT_LINK,
                debit_at=None,
                notify_at=NOW - timedelta(hours=48 * (3 - i)),
            )
            for i in range(3)
        )
    },
    passed=False,
)
add_case(
    "r010_exactly_48h_since_last_boundary",
    R010_MESSAGE_FREQUENCY,
    attempt=_msg_attempt | {"notify_at": NOW},
    context={
        "prior_attempts": (
            _attempt(action_type=ActionType.CONTACT_LINK, debit_at=None, notify_at=NOW - timedelta(hours=48)),
        )
    },
    passed=True,
)
add_case(
    "r010_just_under_48h_since_last_boundary",
    R010_MESSAGE_FREQUENCY,
    attempt=_msg_attempt | {"notify_at": NOW},
    context={
        "prior_attempts": (
            _attempt(
                action_type=ActionType.CONTACT_LINK,
                debit_at=None,
                notify_at=NOW - timedelta(hours=48) + timedelta(seconds=1),
            ),
        )
    },
    passed=False,
)

# R011_ISSUER_RATE_LIMIT
add_case(
    "r011_tokens_available",
    R011_ISSUER_RATE_LIMIT,
    context={"issuer_bucket": TokenBucket(capacity=20, refill_per_hour=60, tokens=5, updated_at=NOW)},
    passed=True,
)
add_case(
    "r011_tokens_exhausted",
    R011_ISSUER_RATE_LIMIT,
    context={"issuer_bucket": TokenBucket(capacity=20, refill_per_hour=0, tokens=0.5, updated_at=NOW)},
    passed=False,
)
add_case("r011_no_bucket_configured", R011_ISSUER_RATE_LIMIT, context={"issuer_bucket": None}, passed=True)
add_case(
    "r011_non_debit_exempt",
    R011_ISSUER_RATE_LIMIT,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={"issuer_bucket": TokenBucket(capacity=20, refill_per_hour=0, tokens=0, updated_at=NOW)},
    passed=True,
)

# R012_CUSTOMER_OPT_OUT
add_case("r012_not_opted_out", R012_CUSTOMER_OPT_OUT, passed=True)
add_case(
    "r012_opted_out_silent_retry",
    R012_CUSTOMER_OPT_OUT,
    context={"customer": _customer(opted_out=True)},
    passed=False,
)
add_case(
    "r012_opted_out_message",
    R012_CUSTOMER_OPT_OUT,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={"customer": _customer(opted_out=True)},
    passed=False,
)
add_case(
    "r012_opted_out_stop_allowed",
    R012_CUSTOMER_OPT_OUT,
    attempt={"action_type": ActionType.STOP, "debit_at": None, "notify_at": None},
    context={"customer": _customer(opted_out=True)},
    passed=True,
)

# R013_PROMISE_TO_PAY_SUPPRESSION
add_case("r013_no_promise", R013_PROMISE_TO_PAY_SUPPRESSION, passed=True)
add_case(
    "r013_before_promise_date",
    R013_PROMISE_TO_PAY_SUPPRESSION,
    context={"customer": _customer(promise_to_pay_until=NOW + timedelta(days=1))},
    passed=False,
)
add_case(
    "r013_exactly_at_promise_date_boundary",
    R013_PROMISE_TO_PAY_SUPPRESSION,
    context={"customer": _customer(promise_to_pay_until=NOW)},
    passed=True,
)
add_case(
    "r013_follow_up_already_consumed",
    R013_PROMISE_TO_PAY_SUPPRESSION,
    attempt={"debit_at": NOW + timedelta(hours=1)},
    context={
        "customer": _customer(promise_to_pay_until=NOW),
        "prior_attempts": (_attempt(debit_at=NOW),),
    },
    passed=False,
)
add_case(
    "r013_stop_always_allowed",
    R013_PROMISE_TO_PAY_SUPPRESSION,
    attempt={"action_type": ActionType.STOP, "debit_at": None, "notify_at": None},
    context={"customer": _customer(promise_to_pay_until=NOW + timedelta(days=1))},
    passed=True,
)

# R014_MESSAGE_CONTENT
_good_message = OutboundMessage(
    merchant_name="Acme", amount=Money.from_rupees(1000), debit_date=date(2026, 8, 25), opt_out_included=True
)
add_case(
    "r014_no_message_supplied",
    R014_MESSAGE_CONTENT,
    attempt=_msg_attempt | {"notify_at": NOW},
    context={"message": None},
    passed=False,
)
add_case(
    "r014_complete_message",
    R014_MESSAGE_CONTENT,
    attempt=_msg_attempt | {"notify_at": NOW, "amount": Money.from_rupees(1000)},
    context={"message": _good_message},
    passed=True,
)
add_case(
    "r014_wrong_amount",
    R014_MESSAGE_CONTENT,
    attempt=_msg_attempt | {"notify_at": NOW, "amount": Money.from_rupees(2000)},
    context={"message": _good_message},
    passed=False,
)
add_case(
    "r014_missing_opt_out",
    R014_MESSAGE_CONTENT,
    attempt=_msg_attempt | {"notify_at": NOW, "amount": Money.from_rupees(1000)},
    context={"message": OutboundMessage(merchant_name="Acme", amount=Money.from_rupees(1000), debit_date=date(2026, 8, 25), opt_out_included=False)},
    passed=False,
)
add_case(
    "r014_silent_retry_exempt",
    R014_MESSAGE_CONTENT,
    context={"message": None},
    passed=True,
)

# R015_ISSUER_DOWNTIME_GATE
add_case("r015_issuer_up", R015_ISSUER_DOWNTIME_GATE, context={"issuer_down": False}, passed=True)
add_case("r015_issuer_down", R015_ISSUER_DOWNTIME_GATE, context={"issuer_down": True}, passed=False)
add_case(
    "r015_non_debit_exempt",
    R015_ISSUER_DOWNTIME_GATE,
    attempt={"action_type": ActionType.CONTACT_LINK, "debit_at": None, "notify_at": NOW},
    context={"issuer_down": True},
    passed=True,
)


@pytest.mark.parametrize("name,rule,attempt_overrides,context_overrides,expected_passed", CASES, ids=[c[0] for c in CASES])
def test_rule_case(
    name: str,
    rule: Rule,
    attempt_overrides: dict[str, Any],
    context_overrides: dict[str, Any],
    expected_passed: bool,
) -> None:
    attempt = _attempt(**attempt_overrides)
    context = _context(**context_overrides)
    result = rule(attempt, context)
    assert result.rule_id == rule.rule_id
    assert result.passed is expected_passed
    if not expected_passed:
        assert result.reason


def test_every_rule_has_a_passing_and_failing_case() -> None:
    passed_ids = {rule.rule_id for _, rule, _, _, passed in CASES if passed}
    failed_ids = {rule.rule_id for _, rule, _, _, passed in CASES if not passed}
    all_ids = {rule.rule_id for rule in ALL_RULES}
    assert passed_ids == all_ids, f"missing a passing case for: {all_ids - passed_ids}"
    assert failed_ids == all_ids, f"missing a failing case for: {all_ids - failed_ids}"


def test_every_rule_has_at_least_three_cases() -> None:
    counts = Counter(rule.rule_id for _, rule, _, _, _ in CASES)
    under = {rule_id: n for rule_id, n in counts.items() if n < 3}
    assert not under, f"rules with fewer than 3 cases: {under}"


RULE_IDS_IN_ORDER = [
    "R001_PRE_DEBIT_NOTICE",
    "R002_AFA_THRESHOLD",
    "R003_MAX_SILENT_ATTEMPTS",
    "R004_ATTEMPT_WINDOW",
    "R005_MIN_INTERVAL_SAME_PATH",
    "R006_HARD_DECLINE_NO_RETRY",
    "R007_MANDATE_ACTIVE",
    "R008_MANDATE_CAP",
    "R009_CONTACT_QUIET_HOURS",
    "R010_MESSAGE_FREQUENCY",
    "R011_ISSUER_RATE_LIMIT",
    "R012_CUSTOMER_OPT_OUT",
    "R013_PROMISE_TO_PAY_SUPPRESSION",
    "R014_MESSAGE_CONTENT",
    "R015_ISSUER_DOWNTIME_GATE",
]


def test_all_rules_are_registered_in_order() -> None:
    assert [rule.rule_id for rule in ALL_RULES] == RULE_IDS_IN_ORDER


def test_guard_approves_a_fully_compliant_plan() -> None:
    from vasool.domain.types import RecoveryPlan

    plan = RecoveryPlan(invoice_id="inv_1", attempts=(_attempt(),))
    context = _context(notice_delivered_at=NOW - timedelta(hours=48))
    verdict = ComplianceGuard.evaluate(plan, context)
    assert verdict.approved is True
    assert len(verdict.results) == len(ALL_RULES)


def test_guard_rejects_and_collects_every_rule_result_not_just_the_first_failure() -> None:
    from vasool.domain.types import RecoveryPlan

    plan = RecoveryPlan(invoice_id="inv_1", attempts=(_attempt(),))
    # No notice delivered (R001 fails) AND mandate revoked (R007 fails) simultaneously.
    context = _context(
        notice_delivered_at=None,
        customer=_customer(mandate_state=MandateState.REVOKED),
    )
    verdict = ComplianceGuard.evaluate(plan, context)
    assert verdict.approved is False
    failing_ids = {r.rule_id for r in verdict.results if not r.passed}
    assert "R001_PRE_DEBIT_NOTICE" in failing_ids
    assert "R007_MANDATE_ACTIVE" in failing_ids
