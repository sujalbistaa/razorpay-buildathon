"""BUILD_PLAN.md Phase 9 accept, for RazorpayClient specifically: idempotent writes (a retry
reuses the same Payment Link rather than creating a second one), exponential backoff on
razorpay.errors.ServerError, a circuit breaker that opens after N *exhausted* calls (not N raw
HTTP retries -- see razorpay_client.py's _call docstring) and fails fast while open, and
BadRequestError treated as a genuine rejection rather than something to retry.

Uses a real RazorpayClient with its underlying razorpay.Client resource methods
(payment_link.create, payment.fetch, payment.fetchDownTime) monkeypatched -- not a fake
object standing in for RazorpayClient -- the same pattern test_llm_fallback.py uses for
LLMClient. Constructing razorpay.Client(auth=(...)) makes no network call, so no real
credentials are needed. time.sleep is monkeypatched to a no-op so backoff tests don't pay for
real wall-clock delays.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from razorpay.errors import BadRequestError, ServerError

from vasool.api.store import LiveStore
from vasool.domain.money import Money
from vasool.domain.types import ActionType, Attempt, Invoice, InvoiceCategory, Rail
from vasool.execute.razorpay_client import MAX_CONSECUTIVE_FAILURES, RazorpayClient

NOW = datetime(2026, 6, 1, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("vasool.execute.razorpay_client.time.sleep", lambda _seconds: None)


@pytest.fixture
def store(tmp_path: Path) -> LiveStore:
    return LiveStore(str(tmp_path / "test_live.db"))


@pytest.fixture
def client(store: LiveStore) -> RazorpayClient:
    return RazorpayClient("fake_key_id", "fake_key_secret", store)


def _invoice(invoice_id: str = "pay_1") -> Invoice:
    return Invoice(
        invoice_id=invoice_id, customer_id="cust_1", amount=Money.from_rupees(499),
        category=InvoiceCategory.STANDARD, first_failed_at=NOW,
    )


def _contact_link_attempt(invoice_id: str = "pay_1", attempt_index: int = 0) -> Attempt:
    return Attempt(
        invoice_id=invoice_id, attempt_index=attempt_index, action_type=ActionType.CONTACT_LINK,
        rail=Rail.UPI_AUTOPAY, amount=Money.from_rupees(499), notify_at=NOW, debit_at=None,
    )


def _silent_retry_attempt(invoice_id: str = "pay_1") -> Attempt:
    return Attempt(
        invoice_id=invoice_id, attempt_index=0, action_type=ActionType.SILENT_RETRY,
        rail=Rail.ENACH, amount=Money.from_rupees(499), notify_at=None, debit_at=NOW,
    )


class _FakeResource:
    """Stands in for client.payment_link / client.payment: each call pops the next
    prescribed response or exception off a queue, so a test can script exactly N failures
    followed by a success (or vice versa) without touching the network.
    """

    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, data: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        self.calls.append(data if data is not None else kwargs)
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_contact_link_creates_a_payment_link_and_records_it(client: RazorpayClient, store: LiveStore) -> None:
    fake_create = _FakeResource([{"id": "plink_abc", "short_url": "https://rzp.io/i/abc", "status": "created"}])
    client._client.payment_link.create = fake_create  # type: ignore[method-assign]

    outcome = client.execute(_invoice(), _contact_link_attempt(), NOW, "pay_1:0:contact_link")

    assert outcome.success is False
    assert outcome.failure_event is None
    assert len(fake_create.calls) == 1
    assert fake_create.calls[0]["reference_id"] == "pay_1:0:contact_link"
    stored = store.get_payment_link("pay_1:0:contact_link")
    assert stored is not None
    assert stored.razorpay_payment_link_id == "plink_abc"


def test_contact_link_retry_reuses_the_stored_link_instead_of_creating_a_second_one(
    client: RazorpayClient, store: LiveStore,
) -> None:
    fake_create = _FakeResource([{"id": "plink_abc", "short_url": "https://rzp.io/i/abc", "status": "created"}])
    client._client.payment_link.create = fake_create  # type: ignore[method-assign]
    key = "pay_1:0:contact_link"

    client.execute(_invoice(), _contact_link_attempt(), NOW, key)
    client.execute(_invoice(), _contact_link_attempt(), NOW, key)  # simulated retry, same key

    assert len(fake_create.calls) == 1  # not called a second time


def test_transient_server_errors_are_retried_with_backoff_then_succeed(
    client: RazorpayClient, store: LiveStore,
) -> None:
    fake_create = _FakeResource([
        ServerError("upstream hiccup"), ServerError("upstream hiccup"),
        {"id": "plink_xyz", "short_url": "https://rzp.io/i/xyz", "status": "created"},
    ])
    client._client.payment_link.create = fake_create  # type: ignore[method-assign]

    outcome = client.execute(_invoice(), _contact_link_attempt(), NOW, "pay_1:0:contact_link")

    assert outcome.success is False
    assert outcome.failure_event is None
    assert len(fake_create.calls) == 3
    assert store.get_payment_link("pay_1:0:contact_link") is not None
    assert client.degraded is False  # recovered before the breaker ever opened


def test_bad_request_error_is_not_retried_and_returns_a_failure_event(client: RazorpayClient) -> None:
    fake_create = _FakeResource([BadRequestError("reference_id already exists")])
    client._client.payment_link.create = fake_create  # type: ignore[method-assign]

    outcome = client.execute(_invoice(), _contact_link_attempt(), NOW, "pay_1:0:contact_link")

    assert outcome.success is False
    assert outcome.failure_event is not None
    assert outcome.failure_event.code == "RAZORPAY_BAD_REQUEST"
    assert len(fake_create.calls) == 1  # never retried


def test_circuit_breaker_opens_after_max_consecutive_exhausted_calls_and_then_fails_fast(
    client: RazorpayClient,
) -> None:
    # Each execute() below exhausts its own retry budget (all ServerError) -- one increment
    # to the breaker's consecutive-failure count per call, not per raw HTTP retry.
    for i in range(MAX_CONSECUTIVE_FAILURES):
        always_failing = _FakeResource([ServerError("down")] * 10)
        client._client.payment_link.create = always_failing  # type: ignore[method-assign]
        outcome = client.execute(_invoice(f"pay_{i}"), _contact_link_attempt(f"pay_{i}"), NOW, f"pay_{i}:0:contact_link")
        assert outcome.success is False

    assert client.degraded is True

    # While open, execute() must fail fast (CircuitOpen swallowed) without ever touching the
    # network again.
    never_called = _FakeResource([{"id": "plink_should_not_be_called", "short_url": "x", "status": "created"}])
    client._client.payment_link.create = never_called  # type: ignore[method-assign]
    outcome = client.execute(_invoice("pay_open"), _contact_link_attempt("pay_open"), NOW, "pay_open:0:contact_link")

    assert outcome.success is False
    assert never_called.calls == []


def test_silent_retry_reports_success_when_razorpays_own_retry_already_captured_it(client: RazorpayClient) -> None:
    client._client.payment.fetch = _FakeResource([{"status": "captured"}])  # type: ignore[method-assign]

    outcome = client.execute(_invoice(), _silent_retry_attempt(), NOW, "pay_1:0:silent_retry")

    assert outcome.success is True
    assert outcome.failure_event is None


def test_silent_retry_reports_still_pending_when_no_error_and_not_yet_captured(client: RazorpayClient) -> None:
    client._client.payment.fetch = _FakeResource([{"status": "created", "error_code": None}])  # type: ignore[method-assign]

    outcome = client.execute(_invoice(), _silent_retry_attempt(), NOW, "pay_1:0:silent_retry")

    assert outcome.success is False
    assert outcome.failure_event is None


def test_silent_retry_surfaces_a_failure_event_when_razorpay_reports_one(client: RazorpayClient) -> None:
    client._client.payment.fetch = _FakeResource([{  # type: ignore[method-assign]
        "status": "failed", "error_code": "BAD_REQUEST_ERROR", "error_description": "insufficient funds",
        "error_source": "bank", "error_step": "payment_authorization", "error_reason": "insufficient_funds",
    }])

    outcome = client.execute(_invoice(), _silent_retry_attempt(), NOW, "pay_1:0:silent_retry")

    assert outcome.success is False
    assert outcome.failure_event is not None
    assert outcome.failure_event.reason == "insufficient_funds"


def test_fetch_downtime_maps_known_methods_and_skips_unrecognized_ones(client: RazorpayClient) -> None:
    client._client.payment.fetchDownTime = _FakeResource([{  # type: ignore[method-assign]
        "items": [
            {"id": "down_1", "method": "upi", "begin": 1717200000, "end": None, "severity": "high",
             "scheduled": False, "instrument": {}},
            {"id": "down_2", "method": "card", "begin": 1717200000, "end": 1717203600, "severity": "medium",
             "scheduled": False, "instrument": {"issuer": "HDFC"}},
            {"id": "down_3", "method": "netbanking", "begin": 1717200000, "end": None, "severity": "high",
             "scheduled": False, "instrument": {"bank": "COSB"}},
        ],
    }])

    windows = client.fetch_downtime()

    assert len(windows) == 2  # netbanking skipped -- not a mandate rail we track
    upi_window = next(w for w in windows if w.method.value == "upi_autopay")
    assert upi_window.end is None  # ongoing, unresolved
    card_window = next(w for w in windows if w.method.value == "card")
    assert card_window.issuer == "HDFC"
    assert card_window.end is not None
