"""make live / `python scripts/live_demo.py` — BUILD_PLAN.md Phase 9: one real recovery loop
against a Razorpay TEST MODE account, end to end. Requires real RAZORPAY_KEY_ID /
RAZORPAY_KEY_SECRET test-mode credentials in the environment (never live-mode keys — this
script creates real objects against whatever account the keys point at).

Flow: create a Plan -> create a Subscription -> operator authorizes the mandate in the browser
-> operator triggers a real failed charge from the Dashboard ("Charge as Failure", test mode
only) -> fetch and classify the failed payment -> decide with the real policy layer -> create a
real test-mode Payment Link -> operator pays it -> recovered.

Test-mode limits (BUILD_PLAN.md, BUILD_DOC.md §6.2), so a rerun doesn't quietly fail partway:
  - max 30 Payment Links per business in test mode
  - card tokens are valid for 3 days
  - test charges are only triggerable from the Dashboard, never the API
  - UPI Payment Links (upi_link: true) are NOT supported in test mode at all
    (razorpay.com/docs/api/payments/payment-links/create-upi/) -- this script never sets it.

This script polls Razorpay's fetch APIs rather than consuming real webhooks. Receiving a real
webhook needs a public HTTPS endpoint (ngrok or similar) pointed at a running `make up` server
with the tunnel URL registered as the account's webhook URL in the Dashboard -- real, but out
of scope for a single demo script to set up unattended. Every step BUILD_PLAN.md describes as
"wait for X webhook" is implemented here as "poll fetch until X", called out at each site. This
is also why the script is interactive rather than silent: two steps ("open this URL and
authorize", "use Charge as Failure in the Dashboard") are things only a human at the Dashboard
can do — there's no API for either.

invariant 5 (never import `razorpay` outside execute/razorpay_client.py) is why this script
only ever calls RazorpayClient's public methods, never the razorpay SDK directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from datetime import UTC, datetime
from typing import Any

from vasool.api.store import LiveStore
from vasool.audit.log import AuditLog
from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import CHARGE_ACTION_TYPES, OutboundMessage, RuleContext
from vasool.diagnose.classify import classify_failure
from vasool.domain.money import Money
from vasool.domain.timezones import ist_date
from vasool.domain.types import (
    CustomerProfile,
    Decision,
    FailureEvent,
    FailureSource,
    Invoice,
    InvoiceCategory,
    MandateState,
    Rail,
    RecoveryPlan,
)
from vasool.execute.razorpay_client import RazorpayClient
from vasool.llm.client import LLMClient
from vasool.logging import configure_logging, get_logger
from vasool.policy.base import PolicyContext
from vasool.policy.heuristic import HeuristicPolicy

logger = get_logger(__name__)

POLL_INTERVAL_SECONDS = 5.0
POLL_TIMEOUT_SECONDS = 600.0
DEMO_AMOUNT_PAISE = 49900  # ~499 INR, matching the illustrative billing range in ASSUMPTIONS.md
LIVE_DB_PATH = "live_demo.db"
AUDIT_DB_PATH = "live_demo_audit.db"
POLICY_VERSION = "heuristic:live_demo"


def _snapshot_hash(invoice: Invoice, context: RuleContext) -> str:
    # Same shape as bench/harness.py's _snapshot_hash -- duplicated rather than imported
    # since that one is a private module helper and this script has no prior attempts to
    # include (a live demo is always attempt_index 0).
    payload = {
        "invoice_id": invoice.invoice_id, "attempt_index": 0, "customer_id": context.customer.customer_id,
        "failure_class": context.failure_class.value, "now": context.now.isoformat(),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _parse_source(raw: object) -> FailureSource:
    try:
        return FailureSource(str(raw))
    except ValueError:
        return FailureSource.GATEWAY


def _prompt(message: str) -> str:
    return input(f"\n>>> {message}\n>>> ").strip()


def _poll_until(fetch: Any, is_ready: Any, *, what: str) -> dict[str, Any]:
    deadline = time.monotonic() + POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result: dict[str, Any] = fetch()
        if is_ready(result):
            return result
        print(f"    ... still waiting on {what} (status={result.get('status')!r}); polling again in {POLL_INTERVAL_SECONDS:.0f}s")
        time.sleep(POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"timed out after {POLL_TIMEOUT_SECONDS:.0f}s waiting on {what}")


def main() -> None:
    configure_logging()
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        print("RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set to real Razorpay TEST MODE credentials.")
        sys.exit(1)

    store = LiveStore(LIVE_DB_PATH)
    rzp = RazorpayClient(key_id, key_secret, store)
    llm = LLMClient()

    print("=== Step 1/7: create a Plan ===")
    plan = rzp.create_plan(
        period="monthly", interval=1, item_name="Vasool live demo subscription", amount_paise=DEMO_AMOUNT_PAISE,
    )
    print(f"    plan created: {plan['id']}")

    print("\n=== Step 2/7: create a Subscription ===")
    subscription = rzp.create_subscription(plan_id=plan["id"], total_count=12)
    subscription_id = str(subscription["id"])
    print(f"    subscription created: {subscription_id}")
    print(f"    open this URL and complete the (test-mode) mandate authorization:\n    {subscription['short_url']}")
    _prompt("press Enter once you've completed authorization in the browser")

    print("\n=== Step 3/7: waiting for the subscription to activate (polling, not a webhook) ===")
    subscription = _poll_until(
        lambda: rzp.fetch_subscription(subscription_id), lambda s: s.get("status") == "active",
        what="subscription activation",
    )
    print("    subscription is active.")

    print("\n=== Step 4/7: trigger a real test-mode charge failure ===")
    print("    In the Razorpay Dashboard (test mode), open this subscription and use")
    print("    'Charge as Failure' — a Dashboard test-mode feature, not something this script can call.")
    payment_id = _prompt("paste the resulting failed payment's ID (pay_xxx) from the Dashboard")

    print("\n=== Step 5/7: fetch the failed payment and classify it ===")
    payment = rzp.fetch_payment(payment_id)
    now = datetime.now(UTC)
    event = FailureEvent(
        invoice_id=payment_id, code=str(payment.get("error_code") or "UNKNOWN"),
        description=str(payment.get("error_description") or ""),
        source=_parse_source(payment.get("error_source")), step=str(payment.get("error_step") or "unknown"),
        reason=payment.get("error_reason"), occurred_at=now,
    )
    failure_class = classify_failure(event, llm)
    print(f"    classified as: {failure_class.value}")

    print("\n=== Step 6/7: decide ===")
    # HeuristicPolicy, not LearnedPolicy: a one-off live demo customer has no payday history
    # and no trained hazard model artefact to load -- exactly the state CLAUDE.md's own
    # fallback rule describes ("model file missing -> HeuristicPolicy"), here by construction
    # rather than failure.
    invoice = Invoice(
        invoice_id=payment_id, customer_id=f"cust_{subscription_id}",
        amount=Money(int(payment.get("amount", DEMO_AMOUNT_PAISE))), category=InvoiceCategory.STANDARD,
        first_failed_at=now,
    )
    profile = CustomerProfile(
        customer_id=invoice.customer_id, split="A", language="en", mandate_rail=Rail.UPI_AUTOPAY,
        mandate_state=MandateState.ACTIVE, mandate_max_amount=invoice.amount, issuer="UNKNOWN",
    )
    context = PolicyContext(customer=profile, failure_class=failure_class, now=now)
    plan_result = HeuristicPolicy().plan(invoice, context)

    if not plan_result.attempts:
        print(f"    policy produced no attempts (stop_rule={plan_result.stop_rule}); nothing to execute.")
        return

    first_attempt = min(plan_result.attempts, key=lambda a: a.debit_at or a.notify_at or now)
    message = OutboundMessage(merchant_name="Vasool", amount=first_attempt.amount, debit_date=ist_date(now), opt_out_included=True)
    rule_context = RuleContext(invoice=invoice, customer=profile, failure_class=failure_class, now=now, message=message)
    verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(first_attempt,)), rule_context)
    print(f"    plan's first attempt: {first_attempt.action_type.value}")
    for result in verdict.results:
        print(f"      [{'PASS' if result.passed else 'FAIL'}] {result.rule_id}" + (f" — {result.reason}" if result.reason else ""))

    # Invariant 7: one append-only audit row before the action executes -- a live decision is
    # exactly what this invariant is for, not just the benchmark's simulated ones.
    audit = AuditLog(AUDIT_DB_PATH)
    decision = Decision(
        invoice_id=invoice.invoice_id, attempt_index=first_attempt.attempt_index, decided_at=now,
        input_snapshot_hash=_snapshot_hash(invoice, rule_context), policy_version=POLICY_VERSION,
        compliance_verdict=verdict, chosen_action=first_attempt.action_type,
        expected_value=invoice.amount if first_attempt.debit_at is not None else Money(0),
    )
    audit.record_decision(decision)

    if not verdict.approved:
        print("    ComplianceGuard rejected this attempt; stopping (this is the guard working correctly, not a bug).")
        return
    if first_attempt.action_type not in CHARGE_ACTION_TYPES:
        print("    First attempt isn't a charge action (e.g. a pre-debit notice ahead of a scheduled debit).")
        print("    A one-shot live demo doesn't simulate the passage of days; stopping here with the decision shown above.")
        return

    print("\n=== Step 7/7: execute the plan's first attempt ===")
    outcome = rzp.execute(invoice, first_attempt, now, first_attempt.idempotency_key)
    if first_attempt.action_type.value == "silent_retry":
        print(f"    silent retry check: success={outcome.success}, failure_event={outcome.failure_event}")
        audit.record_outcome(
            invoice.invoice_id, first_attempt.attempt_index, datetime.now(UTC), outcome.success,
            first_attempt.amount.paise if outcome.success else 0,
            outcome.failure_event.reason if outcome.failure_event else None,
        )
        return

    link_row = store.get_payment_link(first_attempt.idempotency_key)
    assert link_row is not None, "CONTACT_LINK execute() always records a PaymentLinkRow on success"
    print(f"    pay here: {link_row.short_url}")
    _prompt("press Enter once you've paid the link")

    _poll_until(
        lambda: rzp.fetch_payment_link(link_row.razorpay_payment_link_id), lambda pl: pl.get("status") == "paid",
        what="payment link payment",
    )
    audit.record_outcome(invoice.invoice_id, first_attempt.attempt_index, datetime.now(UTC), True, first_attempt.amount.paise, None)
    print(f"\nrecovered: {invoice.amount.format_inr()} via {link_row.short_url}")


if __name__ == "__main__":
    main()
