"""/demo/chaos/* — live, in-browser fault injection. Each toggle drives the exact real code
path `vasool.chaos`'s CLI scenarios already verify (both LLM providers' real HTTP clients
forced to raise, HazardModel.load() on a genuinely corrupt file, RazorpayClient's real circuit
breaker tripped by real exhausted calls) against this server's own live state — not a canned
animation. A reset restores normal startup behaviour. Demo-only: never reachable from make
bench or pytest, and the tripped RazorpayClient runs against its own throwaway LiveStore so it
can never leak a fake invoice into the real at-risk queue.
"""

from __future__ import annotations

import os
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from razorpay.errors import ServerError

from vasool.api.store import LiveStore
from vasool.domain.money import Money
from vasool.domain.types import ActionType, Attempt, Invoice, InvoiceCategory, Rail
from vasool.execute.razorpay_client import MAX_CONSECUTIVE_FAILURES, RazorpayClient
from vasool.llm.client import GEMINI_BASE_URL, GROQ_BASE_URL, REQUEST_TIMEOUT_SECONDS, LLMClient
from vasool.logging import get_logger
from vasool.policy.learned import LearnedPolicy
from vasool.sim.world import load_world_config

logger = get_logger(__name__)

router = APIRouter(prefix="/demo/chaos")


def _forced_down_llm_client() -> LLMClient:
    """A real LLMClient whose HTTP calls always raise on *both* providers -- same technique
    chaos.py's scenario_llm_500s uses, applied to this server's actual app.state.llm_client
    instead of a throwaway one, so real code paths (DLQ replay included) genuinely fall back
    while this is on. Both, not just Groq: LLMClient falls back from Groq to Gemini on any
    failure, so leaving Gemini alive would silently absorb the "kill" and the badge would lie.
    """
    client = LLMClient()
    client._groq = httpx.Client(
        base_url=GROQ_BASE_URL, headers={"Authorization": "Bearer demo-chaos-fake-key"}, timeout=REQUEST_TIMEOUT_SECONDS
    )
    client._gemini = httpx.Client(
        base_url=GEMINI_BASE_URL, headers={"x-goog-api-key": "demo-chaos-fake-key"}, timeout=REQUEST_TIMEOUT_SECONDS
    )

    def _raise(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("simulated failure")

    client._groq.post = _raise  # type: ignore[assignment]
    client._gemini.post = _raise  # type: ignore[assignment]
    return client


@router.post("/llm")
def toggle_llm(request: Request) -> RedirectResponse:
    state = request.app.state
    if state.demo_chaos_llm_forced:
        state.llm_client = LLMClient()
        state.demo_chaos_llm_forced = False
    else:
        state.llm_client = _forced_down_llm_client()
        state.demo_chaos_llm_forced = True
    return RedirectResponse(url="/#chaos-panel", status_code=303)


@router.post("/model")
def toggle_model(request: Request) -> RedirectResponse:
    state = request.app.state
    if state.demo_chaos_model_forced:
        state.demo_chaos_model_forced = False
        return RedirectResponse(url="/#chaos-panel", status_code=303)

    with tempfile.TemporaryDirectory() as tmp:
        garbage_path = Path(tmp) / "hazard_model.txt"
        garbage_path.write_bytes(b"this is not a lightgbm model file")
        policy = LearnedPolicy.from_model_path(garbage_path, load_world_config())

    if not policy.degraded:
        logger.warning("demo_chaos_model_toggle_did_not_degrade")
    state.demo_chaos_model_forced = policy.degraded
    return RedirectResponse(url="/#chaos-panel", status_code=303)


def _always_fails(*args: Any, **kwargs: Any) -> None:
    raise ServerError("down")


def _demo_invoice_and_attempt(i: int) -> tuple[Invoice, Attempt]:
    now = datetime.now(UTC)
    invoice = Invoice(
        invoice_id=f"demo_chaos_{i}", customer_id="demo_chaos_cust", amount=Money.from_rupees(499),
        category=InvoiceCategory.STANDARD, first_failed_at=now,
    )
    attempt = Attempt(
        invoice_id=invoice.invoice_id, attempt_index=0, action_type=ActionType.CONTACT_LINK,
        rail=Rail.UPI_AUTOPAY, amount=invoice.amount, notify_at=now, debit_at=None,
    )
    return invoice, attempt


@router.post("/razorpay")
def toggle_razorpay(request: Request) -> RedirectResponse:
    state = request.app.state
    if state.demo_chaos_razorpay_client is not None:
        state.demo_chaos_razorpay_client = None
        return RedirectResponse(url="/#chaos-panel", status_code=303)

    fd, db_path = tempfile.mkstemp(suffix=".db", prefix="vasool_demo_chaos_")
    os.close(fd)
    client = RazorpayClient("demo_key_id", "demo_key_secret", LiveStore(db_path))

    # Real exponential backoff would block this request for tens of seconds across
    # MAX_CONSECUTIVE_FAILURES exhausted calls -- patched to no-op for the duration of this
    # click only, exactly as `make chaos` patches it process-wide for its own run.
    previous_sleep = time.sleep
    time.sleep = lambda _seconds: None
    try:
        for i in range(MAX_CONSECUTIVE_FAILURES):
            client._client.payment_link.create = _always_fails
            invoice, attempt = _demo_invoice_and_attempt(i)
            client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)
    finally:
        time.sleep = previous_sleep

    if not client.degraded:
        logger.warning("demo_chaos_razorpay_toggle_did_not_trip")
        return RedirectResponse(url="/#chaos-panel", status_code=303)
    state.demo_chaos_razorpay_client = client
    return RedirectResponse(url="/#chaos-panel", status_code=303)


@router.post("/reset")
def reset_all(request: Request) -> RedirectResponse:
    state = request.app.state
    state.llm_client = LLMClient()
    state.demo_chaos_llm_forced = False
    state.demo_chaos_model_forced = False
    state.demo_chaos_razorpay_client = None
    return RedirectResponse(url="/#chaos-panel", status_code=303)
