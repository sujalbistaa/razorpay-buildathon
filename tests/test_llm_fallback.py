"""BUILD_PLAN.md Phase 7 accept: with the LLM client raising on every call, the full
benchmark still runs and produces the same recovery numbers.

Uses a real LLMClient with its underlying httpx.Client calls monkeypatched to raise -- not a
fake object standing in for LLMClient -- so this actually exercises LLMClient's own "never
raises to callers" contract (client.py), not just a test double's behavior. No network call
and no real API key are needed: constructing httpx.Client() doesn't touch the network, only
.post() would.

The honest reason the benchmark numbers hold either way: diagnose/rules.py classifies every
event the simulator can produce (sim/world.py's _fail_outcome always sets `reason` to a
FailureClass value), so diagnose/llm_fallback.py is structurally never reached by cohort
generation regardless of whether the LLM works. This is proven directly, not just inferred
from matching totals.
"""

from __future__ import annotations

import httpx
import pytest

from vasool.bench.harness import run_arm
from vasool.comms.generate import generate_message
from vasool.diagnose.classify import classify_failure
from vasool.diagnose.rules import classify as rules_classify
from vasool.domain.money import Money
from vasool.domain.types import ActionType, Attempt, FailureClass
from vasool.execute.simulator_client import SimulatorClient
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.policy.heuristic import HeuristicPolicy
from vasool.sim.cohort import generate_cohort

SEED = 5
N_CUSTOMERS = 40
N_INVOICES = 120
HORIZON_DAYS = 30


@pytest.fixture
def raising_llm_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    """A real LLMClient, in real ("live") mode, whose HTTP calls always raise on *both*
    providers -- the actual failure shape BUILD_PLAN.md Phase 7 means by "the LLM client
    raising on every call," as opposed to a client that was simply never wired up (stub mode).
    Both, not just Groq: LLMClient falls back from Groq to Gemini on any failure, so leaving
    Gemini alive would silently absorb it and this fixture would no longer test what its name
    says it does.
    """
    monkeypatch.setenv("VASOOL_LLM", "live")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-testing")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    client = LLMClient()
    assert client._groq is not None  # confirms it's actually in "live" mode, not stub
    assert client._gemini is not None

    def _raise(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("simulated failure")

    monkeypatch.setattr(client._groq, "post", _raise)
    monkeypatch.setattr(client._gemini, "post", _raise)
    return client


def test_llm_client_never_raises_when_the_underlying_sdk_call_raises(raising_llm_client: LLMClient) -> None:
    result = raising_llm_client.complete(system="s", user="u")
    assert isinstance(result, FallbackTriggered)
    assert result.reason == "connection_error"


def _origin_event(world: object, invoice: object) -> object:
    customer = world.customer(invoice.customer_id)  # type: ignore[attr-defined]
    for probe in range(1, 500):
        attempt = Attempt(
            invoice_id=invoice.invoice_id, attempt_index=-probe, action_type=ActionType.SILENT_RETRY,  # type: ignore[attr-defined]
            rail=customer.mandate_rail, amount=invoice.amount, notify_at=None, debit_at=invoice.first_failed_at,  # type: ignore[attr-defined]
        )
        outcome = world.attempt(invoice, attempt, invoice.first_failed_at)  # type: ignore[attr-defined]
        if not outcome.success:
            assert outcome.failure_event is not None
            return outcome.failure_event
    raise RuntimeError("no genesis failure found")


def test_cohort_classification_never_reaches_the_llm_for_simulated_events() -> None:
    # Direct proof of the structural claim in the module docstring: rules.py alone classifies
    # every event the simulator emits, for every invoice in a real-sized cohort.
    cohort = generate_cohort(seed=SEED, n_customers=200, n_invoices=600, horizon_days=90)
    unclassified = [
        inv.invoice_id for inv in cohort.invoices
        if rules_classify(_origin_event(cohort.world, inv)) is None  # type: ignore[arg-type]
    ]
    assert unclassified == []


def test_cohort_generation_is_identical_with_a_raising_llm_client(raising_llm_client: LLMClient) -> None:
    with_stub = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    with_raising = generate_cohort(
        seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS, llm_client=raising_llm_client
    )
    assert with_stub.origin_failures == with_raising.origin_failures
    assert with_stub.content_hash() == with_raising.content_hash()


def test_full_benchmark_run_is_unaffected_by_a_raising_llm_client(raising_llm_client: LLMClient) -> None:
    cohort_stub = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    cohort_raising = generate_cohort(
        seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS, llm_client=raising_llm_client
    )

    results_stub = run_arm(HeuristicPolicy(), "heuristic:v1", cohort_stub, SimulatorClient(cohort_stub.world), ":memory:")
    results_raising = run_arm(HeuristicPolicy(), "heuristic:v1", cohort_raising, SimulatorClient(cohort_raising.world), ":memory:")

    total_recovered_stub = sum(r.recovered_paise for r in results_stub)
    total_recovered_raising = sum(r.recovered_paise for r in results_raising)
    assert total_recovered_stub == total_recovered_raising
    assert total_recovered_stub > 0  # sanity: this cohort actually recovers something


def test_generate_message_falls_back_to_template_when_the_llm_client_raises(raising_llm_client: LLMClient) -> None:
    from datetime import date

    message = generate_message(
        ActionType.PRE_DEBIT_NOTICE, FailureClass.INSUFFICIENT_FUNDS, "en",
        merchant_name="Vasool", amount=Money.from_rupees(499), debit_date=date(2026, 6, 20), client=raising_llm_client,
    )
    assert message.source == "template"


def test_diagnose_llm_fallback_returns_unknown_when_the_client_raises(raising_llm_client: LLMClient) -> None:
    from datetime import UTC, datetime

    from vasool.domain.types import FailureEvent, FailureSource

    event = FailureEvent(
        invoice_id="inv_1", code="TOTALLY_UNRECOGNIZED", description="never seen before",
        source=FailureSource.GATEWAY, step="authorization", reason=None, occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    result = classify_failure(event, raising_llm_client)
    assert result == FailureClass.UNKNOWN
