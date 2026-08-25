"""BUILD_PLAN.md Phase 8 accept: "A webhook with a bad signature returns 400 and changes
nothing; the same webhook delivered twice causes exactly one state transition." Also covers
the dead-letter path (BUILD_DOC.md §7's "poison queue message") and /admin/dlq/replay.

A minimal FastAPI app -- just webhooks.router and admin.router over a fresh LiveStore -- rather
than vasool.api.main:app, so these tests don't pay for build_seed_data()'s cohort generation
and hazard training on every run. The LLM client is a real LLMClient in stub mode (default
VASOOL_LLM=stub): no network call, and payment.failed classification here goes through
diagnose/rules.py before it would ever reach the LLM anyway.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from vasool.api import admin, webhooks
from vasool.api.store import LiveStore
from vasool.llm.client import LLMClient

SECRET = "test-webhook-secret"


def _signed_headers(body: bytes, event_id: str, *, secret: str = SECRET) -> dict[str, str]:
    signature = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return {"X-Razorpay-Signature": signature, "x-razorpay-event-id": event_id}


def _payment_failed_body(payment_id: str = "pay_1", amount: int = 50000) -> bytes:
    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {
            "id": payment_id, "customer_id": "cust_1", "amount": amount,
            "error_code": "BAD_REQUEST_ERROR", "error_description": "insufficient funds",
            "error_source": "issuer", "error_step": "payment_authorization", "error_reason": "insufficient_funds",
        }}},
    }
    return json.dumps(payload).encode("utf-8")


@pytest.fixture
def store(tmp_path: Path) -> LiveStore:
    return LiveStore(str(tmp_path / "test_live.db"))


@pytest.fixture
def client(store: LiveStore, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("RAZORPAY_WEBHOOK_SECRET", SECRET)
    app = FastAPI()
    app.state.live_store = store
    app.state.llm_client = LLMClient()
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    with TestClient(app) as test_client:
        yield test_client


def test_bad_signature_returns_400_and_changes_nothing(client: TestClient, store: LiveStore) -> None:
    body = _payment_failed_body()
    response = client.post(
        "/webhooks/razorpay", content=body,
        headers={"X-Razorpay-Signature": "not-the-real-signature", "x-razorpay-event-id": "evt_bad"},
    )
    assert response.status_code == 400
    assert store.list_open_invoices() == []
    assert store.list_dead_letters() == []


def test_missing_event_id_returns_400(client: TestClient) -> None:
    body = _payment_failed_body()
    signature = hmac.new(SECRET.encode("utf-8"), body, hashlib.sha256).hexdigest()
    response = client.post("/webhooks/razorpay", content=body, headers={"X-Razorpay-Signature": signature})
    assert response.status_code == 400


def test_payment_failed_classifies_and_records_open_invoice(client: TestClient, store: LiveStore) -> None:
    body = _payment_failed_body(payment_id="pay_2")
    response = client.post("/webhooks/razorpay", content=body, headers=_signed_headers(body, "evt_1"))
    assert response.status_code == 200

    open_invoices = store.list_open_invoices()
    assert len(open_invoices) == 1
    assert open_invoices[0].invoice_id == "pay_2"
    assert open_invoices[0].failure_class == "insufficient_funds"


def test_duplicate_delivery_causes_exactly_one_state_transition(client: TestClient, store: LiveStore) -> None:
    body = _payment_failed_body(payment_id="pay_3")
    headers = _signed_headers(body, "evt_dup")

    first = client.post("/webhooks/razorpay", content=body, headers=headers)
    second = client.post("/webhooks/razorpay", content=body, headers=headers)

    assert first.status_code == 200
    assert second.status_code == 200
    open_invoices = store.list_open_invoices()
    assert len(open_invoices) == 1
    assert store.get_invoice("pay_3") is not None


def test_concurrent_duplicate_delivery_causes_exactly_one_success(store: LiveStore) -> None:
    """The test above only ever delivers the duplicate sequentially -- two client.post() calls
    in a row -- which a naive SELECT-then-INSERT would also pass. LiveStore.try_record_event's
    own docstring claims it's "race-safe against two concurrent deliveries of the same
    webhook" because dedup is a UNIQUE constraint, not a read-then-write check; this proves
    that claim under an actual race (real OS threads released simultaneously via a Barrier,
    against a real SQLite-backed LiveStore) instead of leaving it asserted only in prose.
    """
    n_threads = 20
    barrier = threading.Barrier(n_threads)
    results: list[bool] = []
    results_lock = threading.Lock()

    def _attempt() -> None:
        barrier.wait()
        result = store.try_record_event("evt_race", "payment.failed", {"payment_id": "pay_race"}, datetime.now(UTC))
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=_attempt) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == n_threads
    assert results.count(True) == 1
    assert results.count(False) == n_threads - 1


def test_processing_failure_lands_in_dead_letter_queue_not_dropped_or_crashed(client: TestClient, store: LiveStore) -> None:
    # amount is not int-coercible -> _handle_payment_failed raises deep inside processing;
    # the endpoint must still ack 200 (already did, before the background task runs) and the
    # failure must surface in the DLQ rather than vanish or crash the worker.
    payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_poison", "amount": "not-an-int", "error_code": "X"}}},
    }
    body = json.dumps(payload).encode("utf-8")
    response = client.post("/webhooks/razorpay", content=body, headers=_signed_headers(body, "evt_poison"))

    assert response.status_code == 200
    dead_letters = store.list_dead_letters()
    assert len(dead_letters) == 1
    assert dead_letters[0].event_id == "evt_poison"
    assert store.get_invoice("pay_poison") is None


def test_dlq_replay_marks_replayed_on_success(client: TestClient, store: LiveStore) -> None:
    from datetime import UTC, datetime

    good_payload = {
        "event": "payment.failed",
        "payload": {"payment": {"entity": {"id": "pay_fixed", "amount": 12300, "error_code": "X"}}},
    }
    store.record_dead_letter("evt_fixed", "payment.failed", good_payload, error="synthetic", now=datetime.now(UTC))
    dlq_id = store.list_dead_letters()[0].id
    assert dlq_id is not None

    response = client.post(f"/admin/dlq/replay?dlq_id={dlq_id}", follow_redirects=False)

    assert response.status_code == 303
    assert store.list_dead_letters() == []  # no longer among the unreplayed
    assert store.get_invoice("pay_fixed") is not None


def test_dlq_replay_failure_leaves_original_unreplayed_and_records_new_dead_letter(client: TestClient, store: LiveStore) -> None:
    from datetime import UTC, datetime

    still_poison = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "pay_x", "amount": "nope"}}}}
    store.record_dead_letter("evt_still_poison", "payment.failed", still_poison, error="synthetic", now=datetime.now(UTC))
    dlq_id = store.list_dead_letters()[0].id

    response = client.post(f"/admin/dlq/replay?dlq_id={dlq_id}", follow_redirects=False)

    assert response.status_code == 303
    remaining = store.list_dead_letters()
    assert len(remaining) == 2  # original (still unreplayed) + a fresh row from the failed replay
    assert all(row.replayed is False for row in remaining)
