"""`make chaos` — BUILD_PLAN.md Phase 9. Accept criterion: "make chaos runs the full benchmark
under continuous fault injection and still completes, with every degraded mode visible on the
dashboard and zero compliance violations." Benchmark-scoped, not live-server-scoped: each
scenario below is a fault from BUILD_DOC.md's fault table, verified directly against the same
code paths `make bench` and the live loop use, not a separate chaos-only mock.

Six scenarios: LLM 500s, a corrupt model artefact, Razorpay 5xx (recovered by backoff and,
separately, exhausted into an open circuit breaker), a duplicated webhook delivery, a poison
queue message, and an issuer-downtime backlog drained through the per-issuer token bucket
instead of a thundering herd. Each is independently runnable and prints PASS/FAIL; a nonzero
exit code means something regressed.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anthropic

from vasool.api.store import LiveStore
from vasool.api.webhooks import _process_event
from vasool.bench.harness import run_arm
from vasool.compliance.buckets import TokenBucket
from vasool.compliance.rules import R011_ISSUER_RATE_LIMIT, RuleContext
from vasool.domain.money import Money
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
from vasool.execute.razorpay_client import MAX_CONSECUTIVE_FAILURES, RazorpayClient
from vasool.execute.simulator_client import SimulatorClient
from vasool.llm.client import LLMClient
from vasool.logging import configure_logging, get_logger
from vasool.policy.heuristic import HeuristicPolicy
from vasool.policy.learned import LearnedPolicy
from vasool.sim.cohort import generate_cohort
from vasool.sim.world import load_world_config

logger = get_logger(__name__)

SEED = 7
N_CUSTOMERS = 40
N_INVOICES = 120
HORIZON_DAYS = 30


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    passed: bool
    detail: str


def _raising_llm_client() -> LLMClient:
    """A real LLMClient, in "anthropic" mode, whose SDK calls always raise -- same shape
    tests/test_llm_fallback.py uses. No real API key or network call: constructing
    anthropic.Anthropic() doesn't touch the network, only messages.create()/.parse() would,
    and those are monkeypatched below to raise before ever doing so.
    """
    previous_mode = os.environ.get("VASOOL_LLM")
    os.environ["VASOOL_LLM"] = "anthropic"
    os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-fake-key-for-chaos")
    try:
        client = LLMClient()
    finally:
        if previous_mode is None:
            os.environ.pop("VASOOL_LLM", None)
        else:
            os.environ["VASOOL_LLM"] = previous_mode
    assert client._client is not None

    def _raise(*args: object, **kwargs: object) -> None:
        raise anthropic.APIConnectionError(request=None)  # type: ignore[arg-type]

    client._client.messages.create = _raise  # type: ignore[assignment]
    client._client.messages.parse = _raise  # type: ignore[assignment]
    return client


def scenario_llm_500s() -> ScenarioResult:
    raising_llm = _raising_llm_client()
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS, llm_client=raising_llm)
    results = run_arm(HeuristicPolicy(), "heuristic:chaos", cohort, SimulatorClient(cohort.world), ":memory:")
    total_recovered = sum(r.recovered_paise for r in results)
    if total_recovered <= 0:
        return ScenarioResult("llm_500s", False, "benchmark completed but recovered nothing (unexpected)")
    return ScenarioResult("llm_500s", True, f"benchmark completed with a raising LLM client, recovered {Money(total_recovered).format_inr()}")


def scenario_corrupt_model_artifact() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        garbage_path = Path(tmp) / "hazard_model.txt"
        garbage_path.write_bytes(b"this is not a lightgbm model file")
        policy = LearnedPolicy.from_model_path(garbage_path, load_world_config())
        if not policy.degraded:
            return ScenarioResult("corrupt_model_artifact", False, "LearnedPolicy did not detect the corrupt file")

        cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
        results = run_arm(policy, "learned:chaos", cohort, SimulatorClient(cohort.world), ":memory:")
        total_recovered = sum(r.recovered_paise for r in results)
        if total_recovered <= 0:
            return ScenarioResult("corrupt_model_artifact", False, "degraded fallback ran but recovered nothing")
        return ScenarioResult(
            "corrupt_model_artifact", True,
            f"HazardModel.load() rejected the corrupt file, LearnedPolicy.degraded=True, "
            f"HeuristicPolicy fallback recovered {Money(total_recovered).format_inr()}",
        )


class _FlakyResource:
    def __init__(self, results: list[Any]) -> None:
        self._results = list(results)
        self.call_count = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.call_count += 1
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _chaos_invoice_and_attempt() -> tuple[Invoice, Attempt]:
    now = datetime.now(UTC)
    invoice = Invoice(invoice_id="chaos_pay_1", customer_id="chaos_cust_1", amount=Money.from_rupees(499), category=InvoiceCategory.STANDARD, first_failed_at=now)
    attempt = Attempt(invoice_id=invoice.invoice_id, attempt_index=0, action_type=ActionType.CONTACT_LINK, rail=Rail.UPI_AUTOPAY, amount=invoice.amount, notify_at=now, debit_at=None)
    return invoice, attempt


def scenario_razorpay_5xx_recovers_via_backoff() -> ScenarioResult:
    from razorpay.errors import ServerError

    with tempfile.TemporaryDirectory() as tmp:
        store = LiveStore(str(Path(tmp) / "chaos_live.db"))
        client = RazorpayClient("fake_id", "fake_secret", store)

        flaky = _FlakyResource([ServerError("upstream 500"), ServerError("upstream 500"), {"id": "plink_chaos", "short_url": "https://rzp.io/i/chaos", "status": "created"}])
        client._client.payment_link.create = flaky

        invoice, attempt = _chaos_invoice_and_attempt()
        outcome = client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)
        if client.degraded or outcome.failure_event is not None or flaky.call_count != 3:
            return ScenarioResult("razorpay_5xx_recovers", False, f"degraded={client.degraded}, calls={flaky.call_count}")
        return ScenarioResult("razorpay_5xx_recovers", True, "2 transient ServerErrors absorbed by backoff, 3rd call created the link, breaker never opened")


def scenario_razorpay_circuit_breaker_opens_and_fails_fast() -> ScenarioResult:
    from razorpay.errors import ServerError

    with tempfile.TemporaryDirectory() as tmp:
        store = LiveStore(str(Path(tmp) / "chaos_live.db"))
        client = RazorpayClient("fake_id", "fake_secret", store)

        for i in range(MAX_CONSECUTIVE_FAILURES):
            always_failing = _FlakyResource([ServerError("down")] * 10)
            client._client.payment_link.create = always_failing
            invoice, attempt = _chaos_invoice_and_attempt()
            invoice = invoice.model_copy(update={"invoice_id": f"chaos_pay_{i}"})
            attempt = attempt.model_copy(update={"invoice_id": invoice.invoice_id})
            client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)

        if not client.degraded:
            return ScenarioResult("razorpay_circuit_breaker", False, "breaker did not open after sustained failures")

        never_called = _FlakyResource([{"id": "should_not_be_reached", "short_url": "x", "status": "created"}])
        client._client.payment_link.create = never_called
        invoice, attempt = _chaos_invoice_and_attempt()
        invoice = invoice.model_copy(update={"invoice_id": "chaos_pay_open"})
        attempt = attempt.model_copy(update={"invoice_id": invoice.invoice_id})
        outcome = client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)

        if never_called.call_count != 0 or outcome.failure_event is not None:
            return ScenarioResult("razorpay_circuit_breaker", False, "breaker open but still called the network, or raised to the caller")
        return ScenarioResult("razorpay_circuit_breaker", True, f"breaker opened after {MAX_CONSECUTIVE_FAILURES} exhausted calls; the next call failed fast with zero network calls")


def scenario_duplicate_webhook_delivery() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveStore(str(Path(tmp) / "chaos_live.db"))
        now = datetime.now(UTC)
        first = store.try_record_event("chaos_evt_1", "payment.failed", {"event": "payment.failed"}, now)
        second = store.try_record_event("chaos_evt_1", "payment.failed", {"event": "payment.failed"}, now)
        if not (first and not second):
            return ScenarioResult("duplicate_webhook_delivery", False, f"first={first}, second={second} (expected True, False)")
        return ScenarioResult("duplicate_webhook_delivery", True, "same x-razorpay-event-id delivered twice caused exactly one recorded transition")


def scenario_poison_queue_message() -> ScenarioResult:
    with tempfile.TemporaryDirectory() as tmp:
        store = LiveStore(str(Path(tmp) / "chaos_live.db"))
        llm = LLMClient()
        poison_payload = {"event": "payment.failed", "payload": {"payment": {"entity": {"id": "chaos_poison", "amount": "not-an-int"}}}}
        try:
            _process_event(store, llm, "chaos_evt_poison", "payment.failed", poison_payload)
        except Exception as exc:  # noqa: BLE001 -- this scenario exists specifically to prove _process_event never lets this happen
            return ScenarioResult("poison_queue_message", False, f"_process_event raised instead of dead-lettering: {exc}")

        dead_letters = store.list_dead_letters()
        if len(dead_letters) != 1 or store.get_invoice("chaos_poison") is not None:
            return ScenarioResult("poison_queue_message", False, f"expected exactly 1 dead letter and no invoice row, got {len(dead_letters)} dead letters")
        return ScenarioResult("poison_queue_message", True, "malformed payload landed in the DLQ, never crashed the worker, never wrote a bad invoice row")


def scenario_issuer_downtime_backlog_drains_via_token_bucket() -> ScenarioResult:
    now = datetime.now(UTC)
    profile = CustomerProfile(customer_id="chaos_cust_backlog", split="A", language="en", mandate_rail=Rail.UPI_AUTOPAY, mandate_state=MandateState.ACTIVE, mandate_max_amount=Money.from_rupees(15000), issuer="HDFC")
    bucket = TokenBucket(capacity=5, refill_per_hour=60, tokens=5, updated_at=now)  # issuer just came back from downtime with a backlog queued

    approved = 0
    for i in range(20):  # a 20-invoice backlog all wanting to retry the instant the issuer is back
        attempt = Attempt(invoice_id=f"chaos_backlog_{i}", attempt_index=0, action_type=ActionType.SILENT_RETRY, rail=Rail.UPI_AUTOPAY, amount=Money.from_rupees(499), notify_at=None, debit_at=now)
        invoice = Invoice(invoice_id=attempt.invoice_id, customer_id=profile.customer_id, amount=attempt.amount, category=InvoiceCategory.STANDARD, first_failed_at=now)
        context = RuleContext(invoice=invoice, customer=profile, failure_class=FailureClass.INSUFFICIENT_FUNDS, now=now, issuer_bucket=bucket)
        result = R011_ISSUER_RATE_LIMIT(attempt, context)
        if result.passed:
            approved += 1
            bucket = bucket.consume(now)

    if approved != 5:
        return ScenarioResult("issuer_downtime_backlog", False, f"expected exactly the bucket's capacity (5) approved out of 20, got {approved}")
    return ScenarioResult("issuer_downtime_backlog", True, "20-attempt backlog throttled to the bucket's capacity (5) instead of a thundering herd")


SCENARIOS = (
    scenario_llm_500s,
    scenario_corrupt_model_artifact,
    scenario_razorpay_5xx_recovers_via_backoff,
    scenario_razorpay_circuit_breaker_opens_and_fails_fast,
    scenario_duplicate_webhook_delivery,
    scenario_poison_queue_message,
    scenario_issuer_downtime_backlog_drains_via_token_bucket,
)


def main() -> None:
    configure_logging()
    # The Razorpay scenarios below exercise real retry-count and circuit-breaker-state
    # behavior, not wall-clock timing -- patched process-wide so `make chaos` finishes in
    # seconds rather than paying for razorpay_client.py's real exponential backoff.
    time.sleep = lambda _seconds: None
    results = []
    for scenario in SCENARIOS:
        try:
            result = scenario()
        except Exception as exc:  # noqa: BLE001 -- a scenario itself blowing up IS a chaos-mode failure worth reporting, not something to let crash the run
            result = ScenarioResult(scenario.__name__, False, f"scenario raised: {exc}")
        results.append(result)
        status = "PASS" if result.passed else "FAIL"
        print(f"[{status}] {result.name}: {result.detail}")

    failures = [r for r in results if not r.passed]
    print(f"\n{len(results) - len(failures)}/{len(results)} chaos scenarios passed.")
    if failures:
        sys.exit(1)


if __name__ == "__main__":
    main()
