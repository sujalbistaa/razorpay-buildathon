from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from vasool.domain.money import Money
from vasool.domain.types import (
    ActionType,
    Attempt,
    FailureEvent,
    FailureSource,
    Invoice,
    InvoiceCategory,
    Rail,
)

NOW = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)


def test_failure_event_rejects_naive_datetime() -> None:
    with pytest.raises(ValidationError):
        FailureEvent(
            invoice_id="inv_1",
            code="BAD_REQUEST_ERROR",
            description="x",
            source=FailureSource.BANK,
            step="payment_authorization",
            occurred_at=datetime(2026, 8, 22, 12, 0),  # noqa: DTZ001 — naive on purpose
        )


def test_invoice_holds_money_not_a_float() -> None:
    invoice = Invoice(
        invoice_id="inv_1",
        customer_id="cust_1",
        amount=Money.from_rupees(999),
        category=InvoiceCategory.STANDARD,
        first_failed_at=NOW,
    )
    assert isinstance(invoice.amount, Money)
    assert invoice.amount.paise == 99900


def test_attempt_idempotency_key_is_deterministic() -> None:
    attempt = Attempt(
        invoice_id="inv_1",
        attempt_index=2,
        action_type=ActionType.SILENT_RETRY,
        rail=Rail.UPI_AUTOPAY,
        amount=Money.from_rupees(500),
        notify_at=NOW,
        debit_at=NOW,
    )
    same_attempt = attempt.model_copy()
    assert attempt.idempotency_key == same_attempt.idempotency_key == "inv_1:2:silent_retry"


def test_models_are_frozen() -> None:
    invoice = Invoice(
        invoice_id="inv_1",
        customer_id="cust_1",
        amount=Money.from_rupees(100),
        category=InvoiceCategory.STANDARD,
        first_failed_at=NOW,
    )
    with pytest.raises(ValidationError):
        invoice.amount = Money.from_rupees(1)  # type: ignore[misc]
