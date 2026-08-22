"""Runs one policy arm over a cohort: for each invoice, plan once, then execute the plan's
attempts one at a time in chronological order through ComplianceGuard and the Executor.

Attempts are evaluated one at a time — not the whole plan in one ComplianceGuard.evaluate()
call — because R001_PRE_DEBIT_NOTICE checks context.notice_delivered_at, an execution fact:
whether a PRE_DEBIT_NOTICE attempt earlier in this same plan actually ran, not whether some
later attempt's own notify_at "looks" 24h out. So the harness accumulates notice_delivered_at
and prior_attempts as it goes, exactly as a live system would.

Every decision is written to the audit log before its attempt executes (invariant 7). An
attempt that fails ComplianceGuard is never executed — compliance_violations in the metrics
counts attempts that executed despite non-compliance, which this harness makes structurally
impossible; it should always be zero. blocked_attempts counts the normal, expected case of
the guard doing its job.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from vasool.audit.log import AuditLog
from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import (
    CHARGE_ACTION_TYPES,
    MESSAGE_ACTION_TYPES,
    OutboundMessage,
    RuleContext,
)
from vasool.domain.money import Money
from vasool.domain.taxonomy import DEFAULT_SOURCE
from vasool.domain.timezones import ist_date
from vasool.domain.types import (
    ActionType,
    Attempt,
    Decision,
    FailureClass,
    FailureSource,
    Invoice,
    RecoveryPlan,
)
from vasool.execute.protocol import AttemptOutcome, Executor
from vasool.policy.base import Policy, PolicyContext
from vasool.policy.payday import PaydayObservation
from vasool.sim.cohort import Cohort

# Not from a merchant record yet — Phase 8's dashboard/onboarding would supply this.
# A placeholder is fine here: R014 checks the field is *present*, not what it says.
PLACEHOLDER_MERCHANT_NAME = "Vasool"


@dataclass(frozen=True)
class InvoiceRunResult:
    invoice_id: str
    recovered: bool
    recovered_paise: int
    attempts_made: int
    silent_attempts_made: int
    messages_sent: int
    false_dunning_messages: int
    days_to_recovery: float | None
    blocked_attempts: int
    compliance_violations: int


def _default_message(invoice: Invoice, attempt: Attempt) -> OutboundMessage:
    send_at = attempt.notify_at or attempt.debit_at
    assert send_at is not None
    return OutboundMessage(
        merchant_name=PLACEHOLDER_MERCHANT_NAME,
        amount=attempt.amount,
        debit_date=ist_date(send_at),
        opt_out_included=True,
    )


def _snapshot_hash(invoice: Invoice, context: RuleContext) -> str:
    payload = {
        "invoice_id": invoice.invoice_id,
        "attempt_index": len(context.prior_attempts),
        "customer_id": context.customer.customer_id,
        "failure_class": context.failure_class.value,
        "now": context.now.isoformat(),
        "prior_attempts": [a.idempotency_key for a in context.prior_attempts],
    }
    blob = json.dumps(payload, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _invoices_by_customer(cohort: Cohort) -> dict[str, list[Invoice]]:
    index: dict[str, list[Invoice]] = {}
    for invoice in cohort.invoices:
        index.setdefault(invoice.customer_id, []).append(invoice)
    return index


def _payday_evidence_for(invoice: Invoice, siblings: list[Invoice], cohort: Cohort) -> tuple[PaydayObservation, ...]:
    return tuple(
        PaydayObservation(
            day_of_month=ist_date(sibling.first_failed_at).day,
            insufficient_funds=cohort.origin_failures[sibling.invoice_id] is FailureClass.INSUFFICIENT_FUNDS,
        )
        for sibling in siblings
        if sibling.invoice_id != invoice.invoice_id
    )


def run_arm(policy: Policy, policy_version: str, cohort: Cohort, executor: Executor, audit_db_path: str) -> list[InvoiceRunResult]:
    audit = AuditLog(audit_db_path)
    by_customer = _invoices_by_customer(cohort)
    return [
        _run_invoice(policy, policy_version, invoice, cohort, executor, audit, by_customer[invoice.customer_id])
        for invoice in cohort.invoices
    ]


def _run_invoice(
    policy: Policy,
    policy_version: str,
    invoice: Invoice,
    cohort: Cohort,
    executor: Executor,
    audit: AuditLog,
    sibling_invoices: list[Invoice],
) -> InvoiceRunResult:
    customer = cohort.world.customer(invoice.customer_id)
    profile = customer.to_profile()
    failure_class = cohort.origin_failures[invoice.invoice_id]

    plan_context = PolicyContext(
        customer=profile,
        failure_class=failure_class,
        now=invoice.first_failed_at,
        payday_evidence=_payday_evidence_for(invoice, sibling_invoices, cohort),
        known_downtime_windows=cohort.world.downtime_windows_known_by(invoice.first_failed_at),
    )
    plan = policy.plan(invoice, plan_context)
    ordered = sorted(plan.attempts, key=lambda a: a.debit_at or a.notify_at or invoice.first_failed_at)

    prior_attempts: list[Attempt] = []
    notice_delivered_at: datetime | None = None
    attempts_made = 0
    silent_attempts_made = 0
    messages_sent = 0
    false_dunning_messages = 0
    blocked_attempts = 0
    compliance_violations = 0
    recovered = False
    recovered_paise = 0
    days_to_recovery: float | None = None

    is_false_dunning_origin = DEFAULT_SOURCE[failure_class] in (
        FailureSource.BANK,
        FailureSource.GATEWAY,
        FailureSource.RAZORPAY,
    )

    for attempt in ordered:
        t = attempt.debit_at or attempt.notify_at
        assert t is not None
        is_message = attempt.action_type in MESSAGE_ACTION_TYPES
        message = _default_message(invoice, attempt) if is_message else None
        issuer_down = cohort.world.is_issuer_down(customer.issuer, attempt.rail, t)

        rule_context = RuleContext(
            invoice=invoice,
            customer=profile,
            failure_class=failure_class,
            now=t,
            prior_attempts=tuple(prior_attempts),
            notice_delivered_at=notice_delivered_at,
            issuer_down=issuer_down,
            issuer_bucket=None,
            message=message,
        )
        verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,)), rule_context)

        expected_value = invoice.amount if attempt.debit_at is not None else Money(0)
        decision = Decision(
            invoice_id=invoice.invoice_id,
            attempt_index=attempt.attempt_index,
            decided_at=t,
            input_snapshot_hash=_snapshot_hash(invoice, rule_context),
            policy_version=policy_version,
            compliance_verdict=verdict,
            chosen_action=attempt.action_type,
            expected_value=expected_value,
        )
        audit.record_decision(decision)  # before the action executes -- invariant 7

        if not verdict.approved:
            blocked_attempts += 1
            break  # a rejected attempt invalidates whatever the rest of the plan assumed

        is_charge = attempt.action_type in CHARGE_ACTION_TYPES
        if is_charge:
            outcome = executor.execute(invoice, attempt, t, attempt.idempotency_key)
        else:
            # PRE_DEBIT_NOTICE / CREDENTIAL_UPDATE_REQUEST / STOP never move money -- never
            # resolved through the Executor's payment logic, or a pure notification could
            # spuriously "succeed" and look like a recovery.
            outcome = AttemptOutcome(success=True, failure_event=None)

        audit.record_outcome(
            invoice_id=invoice.invoice_id,
            attempt_index=attempt.attempt_index,
            recorded_at=t,
            success=outcome.success,
            recovered_paise=attempt.amount.paise if (is_charge and outcome.success) else 0,
            failure_reason=outcome.failure_event.reason if outcome.failure_event else None,
        )

        prior_attempts.append(attempt)
        attempts_made += 1
        if attempt.action_type is ActionType.SILENT_RETRY:
            silent_attempts_made += 1
        if attempt.action_type is ActionType.PRE_DEBIT_NOTICE:
            notice_delivered_at = attempt.notify_at
        if is_message:
            messages_sent += 1
            if is_false_dunning_origin:
                false_dunning_messages += 1

        if is_charge and outcome.success:
            recovered = True
            recovered_paise = attempt.amount.paise
            days_to_recovery = (t - invoice.first_failed_at).total_seconds() / 86400
            break

    return InvoiceRunResult(
        invoice_id=invoice.invoice_id,
        recovered=recovered,
        recovered_paise=recovered_paise,
        attempts_made=attempts_made,
        silent_attempts_made=silent_attempts_made,
        messages_sent=messages_sent,
        false_dunning_messages=false_dunning_messages,
        days_to_recovery=days_to_recovery,
        blocked_attempts=blocked_attempts,
        compliance_violations=compliance_violations,
    )
