"""World.attempt()'s resolution order (BUILD_DOC.md §3.3), tested directly against crafted
LatentCustomer fixtures rather than through the full cohort generator.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from vasool.domain.money import Money
from vasool.domain.types import (
    ActionType,
    Attempt,
    DowntimeWindow,
    FailureClass,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
    Severity,
)
from vasool.sim.world import (
    CardState,
    CustomerGenerator,
    IssuerAvailability,
    LatentCustomer,
    World,
    load_world_config,
)

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
CONFIG = load_world_config()
NO_DOWNTIME = IssuerAvailability.from_windows(())


def _customer(**overrides: Any) -> LatentCustomer:
    defaults: dict[str, Any] = {
        "customer_id": "cust_1",
        "split": "A",
        "language": "en",
        "payday_dom": 1,
        "salary": Money.from_rupees(50_000),
        "spend_rate": 0.02,
        "buffer": Money.from_rupees(1_000),
        "card_state": CardState.VALID,
        "mandate_rail": Rail.UPI_AUTOPAY,
        "mandate_state": MandateState.ACTIVE,
        "mandate_max_amount": Money.from_rupees(20_000),
        "issuer": "HDFC",
        "base_response_rate": 0.3,
        "fatigue_decay": 0.7,
        "intent_to_pay": True,
    }
    defaults.update(overrides)
    return LatentCustomer(**defaults)


def _invoice(**overrides: Any) -> Invoice:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1",
        "customer_id": "cust_1",
        "amount": Money.from_rupees(500),
        "category": InvoiceCategory.STANDARD,
        "first_failed_at": NOW - timedelta(days=1),
    }
    defaults.update(overrides)
    return Invoice(**defaults)


def _attempt(**overrides: Any) -> Attempt:
    defaults: dict[str, Any] = {
        "invoice_id": "inv_1",
        "attempt_index": 0,
        "action_type": ActionType.SILENT_RETRY,
        "rail": Rail.UPI_AUTOPAY,
        "amount": Money.from_rupees(500),
        "notify_at": None,
        "debit_at": NOW,
    }
    defaults.update(overrides)
    return Attempt(**defaults)


def _world(customer: LatentCustomer, *, seed: int = 1, downtime: IssuerAvailability = NO_DOWNTIME) -> World:
    return World({customer.customer_id: customer}, downtime, CONFIG, seed)


def _downtime(rail: Rail, severity: Severity, issuer: str = "HDFC") -> IssuerAvailability:
    window = DowntimeWindow(
        issuer=issuer, method=rail, severity=severity, begin=NOW - timedelta(hours=1), end=NOW + timedelta(hours=1)
    )
    return IssuerAvailability.from_windows((window,))


def test_mandate_revoked_takes_priority_over_everything() -> None:
    customer = _customer(mandate_state=MandateState.REVOKED, card_state=CardState.EXPIRED)
    outcome = _world(customer).attempt(_invoice(), _attempt(), NOW)
    assert outcome.success is False
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.MANDATE_REVOKED.value


def test_mandate_paused_is_distinguished_from_revoked() -> None:
    customer = _customer(mandate_state=MandateState.PAUSED)
    outcome = _world(customer).attempt(_invoice(), _attempt(), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.MANDATE_PAUSED.value


def test_card_expired() -> None:
    customer = _customer(card_state=CardState.EXPIRED)
    outcome = _world(customer).attempt(_invoice(), _attempt(), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.CARD_EXPIRED.value


def test_card_blocked() -> None:
    customer = _customer(card_state=CardState.BLOCKED)
    outcome = _world(customer).attempt(_invoice(), _attempt(), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.DEBIT_INSTRUMENT_BLOCKED.value


def test_issuer_downtime_resolves_to_gateway_technical_error_on_card_rail() -> None:
    customer = _customer(mandate_rail=Rail.CARD)
    world = _world(customer, downtime=_downtime(Rail.CARD, Severity.HIGH))
    outcome = world.attempt(_invoice(), _attempt(rail=Rail.CARD), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.GATEWAY_TECHNICAL_ERROR.value


def test_issuer_downtime_resolves_to_remitter_bank_down_on_upi_rail() -> None:
    customer = _customer(mandate_rail=Rail.UPI_AUTOPAY)
    world = _world(customer, downtime=_downtime(Rail.UPI_AUTOPAY, Severity.HIGH))
    outcome = world.attempt(_invoice(), _attempt(rail=Rail.UPI_AUTOPAY), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.REMITTER_BANK_DOWN.value


def test_low_severity_downtime_does_not_gate() -> None:
    customer = _customer()
    world = _world(customer, downtime=_downtime(Rail.UPI_AUTOPAY, Severity.LOW))
    outcome = world.attempt(_invoice(), _attempt(), NOW)
    assert outcome.failure_event is None or outcome.failure_event.reason != FailureClass.REMITTER_BANK_DOWN.value


def test_amount_over_mandate_cap() -> None:
    customer = _customer(mandate_max_amount=Money.from_rupees(100))
    invoice = _invoice(amount=Money.from_rupees(500))
    outcome = _world(customer).attempt(invoice, _attempt(amount=Money.from_rupees(500)), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.TRANSACTION_LIMIT_EXCEEDED.value


def test_amount_over_afa_free_limit_on_silent_retry() -> None:
    customer = _customer(mandate_max_amount=Money.from_rupees(50_000))
    invoice = _invoice(amount=Money.from_rupees(20_000))
    attempt = _attempt(amount=Money.from_rupees(20_000), action_type=ActionType.SILENT_RETRY)
    outcome = _world(customer).attempt(invoice, attempt, NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.AFA_REQUIRED.value


def test_amount_over_afa_free_limit_but_not_silent_is_allowed_past_this_check() -> None:
    customer = _customer(mandate_max_amount=Money.from_rupees(50_000), buffer=Money.from_rupees(30_000))
    invoice = _invoice(amount=Money.from_rupees(20_000))
    attempt = _attempt(amount=Money.from_rupees(20_000), action_type=ActionType.CONTACT_LINK)
    outcome = _world(customer).attempt(invoice, attempt, NOW)
    assert outcome.failure_event is None or outcome.failure_event.reason != FailureClass.AFA_REQUIRED.value


def test_insufficient_funds_when_balance_below_amount() -> None:
    customer = _customer(salary=Money.from_rupees(100), buffer=Money.from_rupees(0), spend_rate=0.5, payday_dom=1)
    invoice = _invoice(amount=Money.from_rupees(10_000), first_failed_at=NOW - timedelta(days=200))
    attempt = _attempt(amount=Money.from_rupees(10_000))
    outcome = _world(customer).attempt(invoice, attempt, NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.INSUFFICIENT_FUNDS.value


def test_churned_customer_never_has_balance() -> None:
    customer = _customer(intent_to_pay=False, salary=Money.from_rupees(10_00_000), buffer=Money.from_rupees(5_00_000))
    invoice = _invoice(amount=Money.from_rupees(10))
    outcome = _world(customer).attempt(invoice, _attempt(amount=Money.from_rupees(10)), NOW)
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == FailureClass.INSUFFICIENT_FUNDS.value


def test_success_is_deterministic_for_the_same_seed_invoice_and_attempt_index() -> None:
    customer = _customer(buffer=Money.from_rupees(10_000), salary=Money.from_rupees(50_000))
    invoice = _invoice(amount=Money.from_rupees(100))
    attempt = _attempt(amount=Money.from_rupees(100))
    outcome_a = _world(customer, seed=7).attempt(invoice, attempt, NOW)
    outcome_b = _world(customer, seed=7).attempt(invoice, attempt, NOW)
    assert outcome_a.success == outcome_b.success


def test_different_seeds_can_produce_different_outcomes_for_the_same_query() -> None:
    customer = _customer(buffer=Money.from_rupees(10_000), salary=Money.from_rupees(50_000))
    invoice = _invoice(amount=Money.from_rupees(100))
    attempt = _attempt(amount=Money.from_rupees(100))
    outcomes = {_world(customer, seed=s).attempt(invoice, attempt, NOW).success for s in range(20)}
    assert outcomes == {True, False}


def test_world_snapshot_restore_round_trips() -> None:
    world = _world(_customer())
    snap = world.snapshot()
    assert world.restore(snap) is snap


def test_customer_generator_uses_only_the_seeded_rng() -> None:
    config = load_world_config()
    rng_a = np.random.default_rng(99)
    rng_b = np.random.default_rng(99)
    customer_a = CustomerGenerator(config, rng_a).generate("cust_x")
    customer_b = CustomerGenerator(config, rng_b).generate("cust_x")
    assert customer_a == customer_b
