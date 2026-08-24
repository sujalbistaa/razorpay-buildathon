"""Property-based, not table-driven: tests/test_compliance.py enumerates specific cases by
hand; this generates hundreds of randomized inputs per run and checks that four
business-critical rules hold *regardless* of what every other randomized field happens to be.
That's a different failure mode than the table-driven suite catches — a rule that's correct
on every case someone thought to write down but wrong on some combination nobody enumerated.

Each property isolates one rule directly (calling the `Rule` itself, not the full
`ComplianceGuard`) so a failure points at exactly which rule broke, not just "some rule did."
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st

from vasool.compliance.constants import AFA_FREE_LIMIT
from vasool.compliance.rules import (
    R002_AFA_THRESHOLD,
    R006_HARD_DECLINE_NO_RETRY,
    R008_MANDATE_CAP,
    R012_CUSTOMER_OPT_OUT,
    RuleContext,
)
from vasool.domain.money import Money
from vasool.domain.taxonomy import HARD_DECLINE_CLASSES
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

hard_decline_classes = st.sampled_from(sorted(HARD_DECLINE_CLASSES, key=str))
rails = st.sampled_from(list(Rail))
mandate_states = st.sampled_from(list(MandateState))
invoice_categories = st.sampled_from(list(InvoiceCategory))
paise = st.integers(min_value=1, max_value=10_000_000_00)  # up to 1 crore
hours_offset = st.integers(min_value=0, max_value=24 * 30).map(lambda h: timedelta(hours=h))


def _invoice(category: InvoiceCategory) -> Invoice:
    return Invoice(
        invoice_id="inv_prop",
        customer_id="cust_prop",
        amount=Money.from_rupees(1000),
        category=category,
        first_failed_at=NOW,
    )


def _customer(*, mandate_state: MandateState, mandate_cap: Money, issuer: str, opted_out: bool) -> CustomerProfile:
    return CustomerProfile(
        customer_id="cust_prop",
        split="A",
        language="en",
        mandate_rail=Rail.UPI_AUTOPAY,
        mandate_state=mandate_state,
        mandate_max_amount=mandate_cap,
        issuer=issuer,
        opted_out=opted_out,
        promise_to_pay_until=None,
    )


@given(
    failure_class=hard_decline_classes,
    action_type=st.sampled_from([ActionType.SILENT_RETRY, ActionType.PRE_DEBIT_NOTICE]),
    rail=rails,
    amount_paise=paise,
    mandate_state=mandate_states,
    delay=hours_offset,
)
@settings(max_examples=200)
def test_hard_decline_never_retries_or_notices(
    failure_class: FailureClass,
    action_type: ActionType,
    rail: Rail,
    amount_paise: int,
    mandate_state: MandateState,
    delay: timedelta,
) -> None:
    """R006, invariant 3's whole point: 'no bypass path, no force=True' -- a hard decline must
    stay rejected no matter what the amount, rail, mandate state, or timing happen to be.
    """
    attempt = Attempt(
        invoice_id="inv_prop",
        attempt_index=0,
        action_type=action_type,
        rail=rail,
        amount=Money(amount_paise),
        notify_at=None,
        debit_at=NOW + delay,
    )
    context = RuleContext(
        invoice=_invoice(InvoiceCategory.STANDARD),
        customer=_customer(
            mandate_state=mandate_state, mandate_cap=Money(amount_paise), issuer="HDFC", opted_out=False
        ),
        failure_class=failure_class,
        now=NOW,
    )
    result = R006_HARD_DECLINE_NO_RETRY(attempt, context)
    assert not result.passed, f"hard decline {failure_class} approved a {action_type} attempt"


@given(
    amount_paise=st.integers(min_value=AFA_FREE_LIMIT.paise + 1, max_value=AFA_FREE_LIMIT.paise + 10_000_000_00),
    rail=rails,
    delay=hours_offset,
)
@settings(max_examples=200)
def test_afa_threshold_always_blocks_silent_retry_above_limit(
    amount_paise: int, rail: Rail, delay: timedelta
) -> None:
    """R002 -- a silent debit above the AFA-free limit must require AFA, regardless of rail
    or timing. STANDARD category keeps the lower ceiling in play (not the elevated one), so
    every generated amount here is unambiguously over it.
    """
    attempt = Attempt(
        invoice_id="inv_prop",
        attempt_index=0,
        action_type=ActionType.SILENT_RETRY,
        rail=rail,
        amount=Money(amount_paise),
        notify_at=None,
        debit_at=NOW + delay,
    )
    context = RuleContext(
        invoice=_invoice(InvoiceCategory.STANDARD),
        customer=_customer(
            mandate_state=MandateState.ACTIVE, mandate_cap=Money(amount_paise), issuer="HDFC", opted_out=False
        ),
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        now=NOW,
    )
    result = R002_AFA_THRESHOLD(attempt, context)
    assert not result.passed, f"₹{amount_paise / 100:.2f} silent retry approved above the AFA-free limit"


@given(
    mandate_cap_paise=paise,
    overage_paise=st.integers(min_value=1, max_value=10_000_000_00),
    action_type=st.sampled_from(list(ActionType)),
    delay=hours_offset,
)
@settings(max_examples=200)
def test_debit_above_mandate_cap_never_passes(
    mandate_cap_paise: int, overage_paise: int, action_type: ActionType, delay: timedelta
) -> None:
    """R008 -- any debit-type attempt (debit_at set) above the customer's own mandate cap must
    be rejected. Only applies when debit_at is set (a pure notify/message has no debit_at and
    R008 correctly no-ops on it), which is why debit_at is fixed rather than randomized here.
    """
    attempt = Attempt(
        invoice_id="inv_prop",
        attempt_index=0,
        action_type=action_type,
        rail=Rail.UPI_AUTOPAY,
        amount=Money(mandate_cap_paise + overage_paise),
        notify_at=None,
        debit_at=NOW + delay,
    )
    context = RuleContext(
        invoice=_invoice(InvoiceCategory.STANDARD),
        customer=_customer(
            mandate_state=MandateState.ACTIVE,
            mandate_cap=Money(mandate_cap_paise),
            issuer="HDFC",
            opted_out=False,
        ),
        failure_class=FailureClass.INSUFFICIENT_FUNDS,
        now=NOW,
    )
    result = R008_MANDATE_CAP(attempt, context)
    assert not result.passed, "attempt above mandate cap was approved"


@given(
    action_type=st.sampled_from([a for a in ActionType if a is not ActionType.STOP]),
    failure_class=st.sampled_from(list(FailureClass)),
    rail=rails,
    amount_paise=paise,
    delay=hours_offset,
)
@settings(max_examples=200)
def test_opted_out_customer_never_gets_a_non_stop_action(
    action_type: ActionType,
    failure_class: FailureClass,
    rail: Rail,
    amount_paise: int,
    delay: timedelta,
) -> None:
    """R012 -- opt-out is absolute: once a customer has opted out, nothing except STOP passes,
    regardless of failure class, rail, amount, or timing.
    """
    attempt = Attempt(
        invoice_id="inv_prop",
        attempt_index=0,
        action_type=action_type,
        rail=rail,
        amount=Money(amount_paise),
        notify_at=None,
        debit_at=NOW + delay,
    )
    context = RuleContext(
        invoice=_invoice(InvoiceCategory.STANDARD),
        customer=_customer(
            mandate_state=MandateState.ACTIVE, mandate_cap=Money(amount_paise), issuer="HDFC", opted_out=True
        ),
        failure_class=failure_class,
        now=NOW,
    )
    result = R012_CUSTOMER_OPT_OUT(attempt, context)
    assert not result.passed, f"opted-out customer's {action_type} attempt was approved"
