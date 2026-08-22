"""RazorpayClient — the Executor implementation against real Razorpay test-mode APIs
(BUILD_PLAN.md Phase 9). CLAUDE.md invariant 5: this is the only file in the repo that
imports `razorpay`. Endpoints, field names and exception classes below are taken from the
razorpay-python SDK source (v2.0.1) and from Razorpay's published docs, not invented:
razorpay.com/docs/api/payments/payment-links/create-standard/,
razorpay.com/docs/api/payments/payment-links/create-upi/,
razorpay.com/docs/api/payments/fetch-with-id/, razorpay.com/docs/api/payments/downtime/entity.

What each Attempt.action_type maps to, and why:

- CONTACT_LINK: the one action this client actually drives end-to-end. Creates a Payment
  Link with `reference_id` set to the attempt's idempotency key. Payment Links are inherently
  asynchronous — the create response only ever reports `status: "created"`, never whether the
  customer has paid — so execute() returns AttemptOutcome(success=False, failure_event=None)
  right after creating the link. Recovery is discovered later, out of band, via the
  payment_link.paid webhook api/webhooks.py already consumes (Phase 8).

  Customer contact info (for Razorpay's own `notify`/`customer` fields) isn't available here
  — the Executor Protocol only carries Invoice and Attempt, neither of which has an email or
  phone number. The link is created without them; relaying `short_url` to the customer is out
  of scope for this client (scripts/live_demo.py does it manually, matching how the rest of
  that demo already requires an operator to relay dashboard state by hand).

  upi_link: true is real functionality but Razorpay's docs are explicit: "UPI Payment Links is
  not supported in Test Mode. Please experience the product in Live Mode." RazorpayClient
  still supports it (`allow_upi_links=True`), but defaults it off, and live_demo.py never
  turns it on since the whole demo runs in test mode.

- SILENT_RETRY: there is no merchant-callable API to force a mandate debit retry — e-NACH and
  UPI Autopay retries run on Razorpay's own schedule, not ours. execute() is honest about that
  limit: it fetches the payment's current status to see whether Razorpay's own retry already
  resolved it, and maps status/error_* onto AttemptOutcome the same way api/webhooks.py's
  payment.failed handler does. This is an observation, not a retry.

Idempotency (invariant 6): Razorpay's Payment Links API has no idempotency-key header — that
exists only for the Payouts and Refunds APIs (X-Payout-Idempotency / X-Refund-Idempotency),
not Payment Links. RazorpayClient enforces it itself via LiveStore's PaymentLinkRow: before
ever calling payment_link.create(), it checks for a row keyed by the same idempotency_key and
reuses that link instead of creating a second one.

Resilience: every call goes through `_call`, which retries razorpay.errors.ServerError and
connection-level failures with exponential backoff, and opens a circuit breaker after
MAX_CONSECUTIVE_FAILURES consecutive failures so a sustained Razorpay outage fails fast
instead of hammering a dead API (BUILD_DOC.md's fault table: "Razorpay API 5xx -> exponential
backoff reusing the same idempotency key; circuit breaker opens after N; attempts queued,
never dropped"). razorpay.errors.BadRequestError is a genuine rejection, not a transient
fault, and is never retried. Real wall-clock time (not the injected Clock policy/ and sim/
use) is appropriate here: this module is the live I/O boundary, not simulated or policy code
— the same reasoning llm/client.py already applies to its own SDK retries.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, TypeVar

import razorpay
import requests
from razorpay.errors import BadRequestError, GatewayError, ServerError

from vasool.api.store import LiveStore
from vasool.domain.types import (
    ActionType,
    Attempt,
    DowntimeWindow,
    FailureEvent,
    FailureSource,
    Invoice,
    Rail,
    Severity,
)
from vasool.execute.protocol import AttemptOutcome
from vasool.logging import get_logger

logger = get_logger(__name__)

REQUEST_TIMEOUT_SECONDS = 20.0
MAX_CONSECUTIVE_FAILURES = 5  # BUILD_DOC.md fault table: "circuit breaker opens after N"
CIRCUIT_RESET_SECONDS = 60.0
INITIAL_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 30.0
MAX_CALL_ATTEMPTS = 4

# Razorpay's downtime `method` values (razorpay.com/docs/api/payments/downtime/entity) are
# card, netbanking, upi -- there's no distinct e-NACH value documented. Only upi and card map
# onto a Rail our mandate model tracks; netbanking and anything unrecognized is skipped rather
# than guessed at.
_METHOD_TO_RAIL: dict[str, Rail] = {"upi": Rail.UPI_AUTOPAY, "card": Rail.CARD}

_RETRIABLE_EXCEPTIONS = (ServerError, requests.exceptions.ConnectionError, requests.exceptions.Timeout)

T = TypeVar("T")


class CircuitOpen(Exception):
    """Raised by _CircuitBreaker.before_call instead of letting a call reach the network."""


@dataclass
class _CircuitBreaker:
    max_consecutive_failures: int = MAX_CONSECUTIVE_FAILURES
    reset_seconds: float = CIRCUIT_RESET_SECONDS
    consecutive_failures: int = field(default=0, init=False)
    opened_at: datetime | None = field(default=None, init=False)

    def before_call(self, now: datetime) -> None:
        if self.opened_at is None:
            return
        if (now - self.opened_at).total_seconds() < self.reset_seconds:
            raise CircuitOpen(f"razorpay circuit open, {self.reset_seconds}s cooldown")
        # Cooldown elapsed: half-open -- let the next call through, closing on success.
        self.opened_at = None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.opened_at = None

    def record_failure(self, now: datetime) -> None:
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.max_consecutive_failures:
            self.opened_at = now

    @property
    def degraded(self) -> bool:
        return self.opened_at is not None


def _parse_source(raw: object) -> FailureSource:
    try:
        return FailureSource(str(raw))
    except ValueError:
        return FailureSource.GATEWAY


class RazorpayClient:
    def __init__(
        self, key_id: str, key_secret: str, live_store: LiveStore, *, allow_upi_links: bool = False,
    ) -> None:
        self._client = razorpay.Client(auth=(key_id, key_secret))
        self._client.set_app_details({"title": "vasool", "version": "0.1.0"})
        self._store = live_store
        self._allow_upi_links = allow_upi_links
        self._breaker = _CircuitBreaker()

    @property
    def degraded(self) -> bool:
        """Read by api/dashboard.py for the `razorpay` degraded-mode badge."""
        return self._breaker.degraded

    def _call(self, fn: Callable[[], T]) -> T:
        # The breaker's consecutive-failure count tracks one increment per exhausted _call()
        # (a whole queued attempt, backoff included), not per raw HTTP retry within it -- a
        # handful of transient 500s inside a single backoff loop is one flaky moment, not N
        # of the "N consecutive failures" BUILD_DOC.md's circuit breaker opens after.
        self._breaker.before_call(datetime.now(UTC))
        delay = INITIAL_BACKOFF_SECONDS
        last_exc: Exception | None = None
        for attempt in range(MAX_CALL_ATTEMPTS):
            try:
                result = fn()
            except _RETRIABLE_EXCEPTIONS as exc:
                last_exc = exc
                if attempt == MAX_CALL_ATTEMPTS - 1:
                    break
                logger.warning("razorpay_call_retrying", attempt=attempt, error=str(exc))
                time.sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
            else:
                self._breaker.record_success()
                return result
        self._breaker.record_failure(datetime.now(UTC))
        assert last_exc is not None  # the loop only exits here via the except branch above
        raise last_exc

    def execute(self, invoice: Invoice, attempt: Attempt, t: datetime, idempotency_key: str) -> AttemptOutcome:
        try:
            if attempt.action_type is ActionType.CONTACT_LINK:
                return self._execute_contact_link(invoice, attempt, idempotency_key)
            if attempt.action_type is ActionType.SILENT_RETRY:
                return self._check_silent_retry(invoice, t)
            raise ValueError(f"RazorpayClient.execute() has no handling for {attempt.action_type!r}")
        except CircuitOpen as exc:
            logger.error("razorpay_circuit_open", invoice_id=invoice.invoice_id, error=str(exc))
            return AttemptOutcome(success=False, failure_event=None)
        except (ServerError, GatewayError, *_RETRIABLE_EXCEPTIONS) as exc:
            # Retries exhausted (ServerError/network) or a bank/gateway-side failure that
            # isn't ours to retry. CLAUDE.md: "never let a failing external dependency take
            # the system down" -- queue-and-back-off happens one layer up (the live webhook
            # loop re-delivers the same idempotency_key), so this never raises to the caller.
            logger.error(
                "razorpay_call_exhausted", invoice_id=invoice.invoice_id,
                action=attempt.action_type.value, error=str(exc),
            )
            return AttemptOutcome(success=False, failure_event=None)
        except BadRequestError as exc:
            logger.error(
                "razorpay_bad_request", invoice_id=invoice.invoice_id,
                action=attempt.action_type.value, error=str(exc),
            )
            event = FailureEvent(
                invoice_id=invoice.invoice_id, code="RAZORPAY_BAD_REQUEST", description=str(exc),
                source=FailureSource.RAZORPAY, step="unknown", reason=None, occurred_at=t,
            )
            return AttemptOutcome(success=False, failure_event=event)

    def _execute_contact_link(self, invoice: Invoice, attempt: Attempt, idempotency_key: str) -> AttemptOutcome:
        existing = self._store.get_payment_link(idempotency_key)
        if existing is not None:
            logger.info(
                "razorpay_payment_link_reused", invoice_id=invoice.invoice_id,
                payment_link_id=existing.razorpay_payment_link_id,
            )
            return AttemptOutcome(success=False, failure_event=None)

        data: dict[str, Any] = {
            "amount": attempt.amount.paise,
            "currency": "INR",
            "reference_id": idempotency_key,
            "description": f"Recovery for invoice {invoice.invoice_id}",
        }
        if self._allow_upi_links:
            data["upi_link"] = True

        response = self._call(lambda: self._client.payment_link.create(data, timeout=REQUEST_TIMEOUT_SECONDS))
        self._store.record_payment_link(
            idempotency_key, invoice.invoice_id, str(response["id"]), str(response["short_url"]), datetime.now(UTC),
        )
        logger.info(
            "razorpay_payment_link_created", invoice_id=invoice.invoice_id,
            payment_link_id=response["id"], short_url=response["short_url"],
        )
        return AttemptOutcome(success=False, failure_event=None)

    def _check_silent_retry(self, invoice: Invoice, t: datetime) -> AttemptOutcome:
        response = self.fetch_payment(invoice.invoice_id)
        if response.get("status") == "captured":
            return AttemptOutcome(success=True, failure_event=None)

        error_code = response.get("error_code")
        if error_code is None:
            return AttemptOutcome(success=False, failure_event=None)  # still pending

        event = FailureEvent(
            invoice_id=invoice.invoice_id, code=str(error_code),
            description=str(response.get("error_description") or ""),
            source=_parse_source(response.get("error_source")),
            step=str(response.get("error_step") or "unknown"),
            reason=response.get("error_reason"), occurred_at=t,
        )
        return AttemptOutcome(success=False, failure_event=event)

    def fetch_payment(self, payment_id: str) -> dict[str, Any]:
        """Public (unlike _execute_contact_link) because scripts/live_demo.py also needs a
        raw payment fetch, and invariant 5 means it can't import `razorpay` and do this
        itself.
        """
        result: dict[str, Any] = self._call(lambda: self._client.payment.fetch(payment_id, timeout=REQUEST_TIMEOUT_SECONDS))
        return result

    def create_plan(self, *, period: str, interval: int, item_name: str, amount_paise: int, currency: str = "INR") -> dict[str, Any]:
        """scripts/live_demo.py's one-time setup step -- not part of the Executor Protocol
        or the recovery loop itself. razorpay.com/docs/payments/subscriptions/create-plans/.
        """
        data = {"period": period, "interval": interval, "item": {"name": item_name, "amount": amount_paise, "currency": currency}}
        result: dict[str, Any] = self._call(lambda: self._client.plan.create(data, timeout=REQUEST_TIMEOUT_SECONDS))
        return result

    def create_subscription(self, *, plan_id: str, total_count: int, customer_notify: bool = True) -> dict[str, Any]:
        """scripts/live_demo.py's one-time setup step. razorpay.com/docs/api/payments/subscriptions/create/."""
        data = {"plan_id": plan_id, "customer_notify": 1 if customer_notify else 0, "total_count": total_count}
        result: dict[str, Any] = self._call(lambda: self._client.subscription.create(data, timeout=REQUEST_TIMEOUT_SECONDS))
        return result

    def fetch_subscription(self, subscription_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            lambda: self._client.subscription.fetch(subscription_id, timeout=REQUEST_TIMEOUT_SECONDS)
        )
        return result

    def fetch_payment_link(self, payment_link_id: str) -> dict[str, Any]:
        result: dict[str, Any] = self._call(
            lambda: self._client.payment_link.fetch(payment_link_id, timeout=REQUEST_TIMEOUT_SECONDS)
        )
        return result

    def fetch_downtime(self) -> tuple[DowntimeWindow, ...]:
        """Not part of the Executor Protocol -- consumed by scripts/live_demo.py to seed a
        live PolicyContext's known_downtime_windows, same role sim/world.py's synthetic
        windows play in the benchmark.
        """
        response = self._call(lambda: self._client.payment.fetchDownTime(timeout=REQUEST_TIMEOUT_SECONDS))
        windows = []
        for item in response.get("items", []):
            rail = _METHOD_TO_RAIL.get(item.get("method"))
            if rail is None:
                continue
            instrument = item.get("instrument") or {}
            issuer = str(instrument.get("issuer") or instrument.get("bank") or "UNKNOWN")
            begin = datetime.fromtimestamp(item["begin"], tz=UTC)
            end_ts = item.get("end")
            end = datetime.fromtimestamp(end_ts, tz=UTC) if end_ts else None
            windows.append(DowntimeWindow(
                issuer=issuer, method=rail, severity=Severity(item.get("severity", "low")),
                begin=begin, end=end, scheduled=bool(item.get("scheduled", False)),
            ))
        return tuple(windows)
