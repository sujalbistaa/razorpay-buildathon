"""POST /webhooks/razorpay — BUILD_PLAN.md Phase 8. Verifies X-Razorpay-Signature as
HMAC-SHA256 hex of the raw request body against RAZORPAY_WEBHOOK_SECRET, in constant time
(hmac.compare_digest). Signature mismatch -> 400, structured log, zero state change: the
signature check happens before the body is ever parsed or a dedup row is written.

Dedupes on x-razorpay-event-id via LiveStore.try_record_event's UNIQUE constraint. Acks fast
(a bare 200, no processing on the request path) and hands the real work to a FastAPI
BackgroundTask -- this stack has no Celery/Redis/Kafka, and an in-process background task is
the boring choice that satisfies "ack within 200ms, enqueue" without one.

A processing failure never crashes the background worker or drops the event silently: it
lands in the dead-letter table for a human to inspect and replay via /admin/dlq/replay
(BUILD_DOC.md §7's "poison queue message" chaos scenario).

Payload shapes follow Razorpay's documented event/payload/entity structure
(razorpay.com/docs/webhooks/validate-test/) and the downtime entity fields specifically
(razorpay.com/docs/api/payments/downtime/entity, already cited in domain/types.py's
DowntimeWindow). One nesting key is inferred rather than independently verified: a
payment.downtime.* webhook's entity is assumed to sit at payload["payload"]["payment_downtime"]
["entity"], following the payload.<entity>.entity convention Razorpay's other webhooks
document -- flagged here rather than asserted as sourced, per CLAUDE.md: "when uncertain about
a Razorpay API detail, say you are uncertain."

Phase 8's scope boundary, deliberately: payment.failed here classifies the failure and records
an open LiveInvoiceRow. It does not run the full EV planner, because a bare payment webhook
carries no payday history, mandate rail, or customer profile -- assembling a real PolicyContext
from live data (a customer lookup) is Phase 9's "Live Razorpay loop," not this one.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request, Response

from vasool.api.store import LiveStore
from vasool.diagnose.classify import classify_failure
from vasool.domain.types import FailureEvent, FailureSource, StopRule
from vasool.llm.client import LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)
router = APIRouter()

WEBHOOK_SECRET_ENV = "RAZORPAY_WEBHOOK_SECRET"

PAID_EVENTS = frozenset({"subscription.charged", "invoice.paid", "payment_link.paid"})
SUBSCRIPTION_STATUS_EVENTS = frozenset({"subscription.pending", "subscription.halted"})
DOWNTIME_STARTED_EVENTS = frozenset({"payment.downtime.started", "payment.downtime.updated"})


def verify_signature(raw_body: bytes, signature: str | None, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@router.post("/webhooks/razorpay")
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_razorpay_signature: str | None = Header(default=None, alias="X-Razorpay-Signature"),
    x_razorpay_event_id: str | None = Header(default=None, alias="x-razorpay-event-id"),
) -> Response:
    raw_body = await request.body()
    secret = os.environ.get(WEBHOOK_SECRET_ENV, "")

    if not verify_signature(raw_body, x_razorpay_signature, secret):
        logger.warning("webhook_signature_mismatch", event_id=x_razorpay_event_id)
        raise HTTPException(status_code=400, detail="signature mismatch")

    if not x_razorpay_event_id:
        logger.warning("webhook_missing_event_id")
        raise HTTPException(status_code=400, detail="missing x-razorpay-event-id header")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        logger.warning("webhook_invalid_json", error=str(exc))
        raise HTTPException(status_code=400, detail="invalid JSON body") from exc

    event_type = str(payload.get("event", ""))
    now = datetime.now(UTC)
    store: LiveStore = request.app.state.live_store

    is_new = store.try_record_event(x_razorpay_event_id, event_type, payload, now)
    if not is_new:
        logger.info("webhook_duplicate_ignored", event_id=x_razorpay_event_id, event_type=event_type)
        return Response(status_code=200)

    background_tasks.add_task(
        _process_event, store, request.app.state.llm_client, x_razorpay_event_id, event_type, payload
    )
    return Response(status_code=200)


def _process_event(store: LiveStore, llm_client: LLMClient, event_id: str, event_type: str, payload: dict[str, Any]) -> None:
    try:
        _dispatch(store, llm_client, event_type, payload)
    except Exception as exc:  # noqa: BLE001 -- a poison message must degrade to the DLQ, never crash the background worker or drop silently
        logger.error("webhook_processing_failed", event_id=event_id, event_type=event_type, error=str(exc))
        store.record_dead_letter(event_id, event_type, payload, error=str(exc), now=datetime.now(UTC))


def _dispatch(store: LiveStore, llm_client: LLMClient, event_type: str, payload: dict[str, Any]) -> None:
    if event_type == "payment.failed":
        _handle_payment_failed(store, llm_client, payload)
    elif event_type in SUBSCRIPTION_STATUS_EVENTS:
        _handle_subscription_status(store, payload, event_type)
    elif event_type in PAID_EVENTS:
        _handle_paid(store, payload)
    elif event_type in DOWNTIME_STARTED_EVENTS:
        _handle_downtime_started(store, payload)
    elif event_type == "payment.downtime.resolved":
        _handle_downtime_resolved(store, payload)
    else:
        logger.info("webhook_unhandled_event_type", event_type=event_type)


def _payment_entity(payload: dict[str, Any]) -> dict[str, Any]:
    entity: dict[str, Any] = payload.get("payload", {}).get("payment", {}).get("entity", {})
    return entity


def _handle_payment_failed(store: LiveStore, llm_client: LLMClient, payload: dict[str, Any]) -> None:
    entity = _payment_entity(payload)
    payment_id = str(entity.get("id", "unknown"))
    customer_id = str(entity.get("customer_id") or entity.get("notes", {}).get("customer_id") or "unknown")
    amount_paise = int(entity.get("amount", 0))

    event = FailureEvent(
        invoice_id=payment_id,
        code=str(entity.get("error_code", "UNKNOWN")),
        description=str(entity.get("error_description", "")),
        source=_parse_source(entity.get("error_source")),
        step=str(entity.get("error_step", "unknown")),
        reason=entity.get("error_reason"),
        occurred_at=datetime.now(UTC),
    )
    failure_class = classify_failure(event, llm_client)
    store.upsert_invoice(
        payment_id, customer_id, amount_paise, status="open", now=datetime.now(UTC),
        failure_class=failure_class.value,
    )
    logger.info("webhook_payment_failed_classified", invoice_id=payment_id, failure_class=failure_class.value)


def _parse_source(raw: object) -> FailureSource:
    try:
        return FailureSource(str(raw))
    except ValueError:
        return FailureSource.GATEWAY


def _handle_subscription_status(store: LiveStore, payload: dict[str, Any], event_type: str) -> None:
    entity = payload.get("payload", {}).get("subscription", {}).get("entity", {})
    subscription_id = str(entity.get("id", "unknown"))
    status = "stopped" if event_type == "subscription.halted" else "open"
    invoice = store.get_invoice(subscription_id)
    amount = invoice.amount_paise if invoice is not None else 0
    customer_id = invoice.customer_id if invoice is not None else "unknown"
    store.upsert_invoice(subscription_id, customer_id, amount, status=status, now=datetime.now(UTC))
    logger.info("webhook_subscription_status", subscription_id=subscription_id, event_type=event_type)


def _handle_paid(store: LiveStore, payload: dict[str, Any]) -> None:
    for entity_key in ("payment", "subscription", "invoice", "payment_link"):
        entity = payload.get("payload", {}).get(entity_key, {}).get("entity", {})
        if entity:
            break
    else:
        entity = {}
    invoice_id = str(entity.get("id", "unknown"))
    existing = store.get_invoice(invoice_id)
    amount_paise = int(entity.get("amount", existing.amount_paise if existing else 0))
    customer_id = str(entity.get("customer_id") or (existing.customer_id if existing else "unknown"))
    store.upsert_invoice(
        invoice_id, customer_id, amount_paise, status="recovered", now=datetime.now(UTC),
        stop_rule=StopRule.INVOICE_PAID.value,
    )
    logger.info("webhook_invoice_paid", invoice_id=invoice_id)


def _downtime_entity(payload: dict[str, Any]) -> dict[str, Any]:
    # See module docstring -- this nesting key is inferred, not independently verified.
    entity: dict[str, Any] = payload.get("payload", {}).get("payment_downtime", {}).get("entity", {})
    return entity


def _handle_downtime_started(store: LiveStore, payload: dict[str, Any]) -> None:
    entity = _downtime_entity(payload)
    issuer = str(entity.get("instrument", {}).get("issuer", "UNKNOWN"))
    method = str(entity.get("method", "unknown"))
    severity = str(entity.get("severity", "low"))
    begin_ts = entity.get("begin")
    begin = datetime.fromtimestamp(begin_ts, tz=UTC) if begin_ts else datetime.now(UTC)
    end_ts = entity.get("end")
    end = datetime.fromtimestamp(end_ts, tz=UTC) if end_ts else None
    store.record_downtime(issuer, method, severity, begin, end, scheduled=bool(entity.get("scheduled", False)))
    logger.info("webhook_downtime_started", issuer=issuer, method=method, severity=severity)


def _handle_downtime_resolved(store: LiveStore, payload: dict[str, Any]) -> None:
    entity = _downtime_entity(payload)
    issuer = str(entity.get("instrument", {}).get("issuer", "UNKNOWN"))
    method = str(entity.get("method", "unknown"))
    store.resolve_downtime(issuer, method, datetime.now(UTC))
    logger.info("webhook_downtime_resolved", issuer=issuer, method=method)
