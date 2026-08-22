"""The four arms `learned` has to beat. Each must produce a plan that passes
ComplianceGuard on its own — the guard doesn't grade a baseline on a curve for being
naive, so every mandate debit here is paired with its own preceding PRE_DEBIT_NOTICE
attempt (R001 checks that a notice was actually delivered, not that debit_at "looks"
24h out — see bench/harness.py for how the two get linked at execution time).
"""

from __future__ import annotations

from datetime import datetime, timedelta

from vasool.compliance.constants import PRE_DEBIT_NOTICE_HOURS
from vasool.domain.types import ActionType, Attempt, Invoice, RecoveryPlan
from vasool.policy.base import PolicyContext

PRE_DEBIT_LEAD = timedelta(hours=PRE_DEBIT_NOTICE_HOURS)


def _notice_and_debit(invoice: Invoice, context: PolicyContext, debit_at: datetime, start_index: int) -> list[Attempt]:
    notify_at = debit_at - PRE_DEBIT_LEAD
    return [
        Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=start_index,
            action_type=ActionType.PRE_DEBIT_NOTICE,
            rail=context.customer.mandate_rail,
            amount=invoice.amount,
            notify_at=notify_at,
            debit_at=None,
        ),
        Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=start_index + 1,
            action_type=ActionType.SILENT_RETRY,
            rail=context.customer.mandate_rail,
            amount=invoice.amount,
            notify_at=None,
            debit_at=debit_at,
        ),
    ]


class NoRetryPolicy:
    """The floor. Diagnoses, never acts."""

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        return RecoveryPlan(invoice_id=invoice.invoice_id, attempts=())


class RazorpayDefaultPolicy:
    """Razorpay Subscriptions' own documented retry schedule: T+1, T+2, T+3 daily, then
    halted. https://razorpay.com/docs/payments/subscriptions/payment-retries/

    Real Razorpay handles the pre-debit notice separately from the retry schedule itself;
    modeled here as one notice per debit so the plan is compliant on its own terms.
    """

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        attempts: list[Attempt] = []
        for day in (1, 2, 3):
            debit_at = invoice.first_failed_at + timedelta(days=day)
            attempts.extend(_notice_and_debit(invoice, context, debit_at, len(attempts)))
        return RecoveryPlan(invoice_id=invoice.invoice_id, attempts=tuple(attempts))


class Static137Policy:
    """The common merchant-configured schedule: retry on day 1, 3 and 7."""

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        attempts: list[Attempt] = []
        for day in (1, 3, 7):
            debit_at = invoice.first_failed_at + timedelta(days=day)
            attempts.extend(_notice_and_debit(invoice, context, debit_at, len(attempts)))
        return RecoveryPlan(invoice_id=invoice.invoice_id, attempts=tuple(attempts))


class DunningOnlyPolicy:
    """Message immediately, never silently retry."""

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        attempt = Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=0,
            action_type=ActionType.CONTACT_LINK,
            rail=context.customer.mandate_rail,
            amount=invoice.amount,
            notify_at=invoice.first_failed_at,
            debit_at=None,
        )
        return RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,))
