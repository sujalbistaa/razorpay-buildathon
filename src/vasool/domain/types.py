"""The failure/attempt/plan vocabulary every other package shares. Facts, so everything is frozen."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict

from vasool.domain.money import Money


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True)


class FailureClass(StrEnum):
    """Razorpay card reasons: https://razorpay.com/docs/errors/payments/cards/
    UPI/NPCI codes: https://razorpay.com/blog/tackling-upi-payment-failures-with-razorpay/
    """

    # Card reasons
    INSUFFICIENT_FUNDS = "insufficient_funds"  # also NPCI Z9
    CARD_EXPIRED = "card_expired"
    CARD_DECLINED = "card_declined"
    DEBIT_INSTRUMENT_BLOCKED = "debit_instrument_blocked"
    DEBIT_INSTRUMENT_INACTIVE = "debit_instrument_inactive"
    CARD_NOT_ENROLLED = "card_not_enrolled"
    AUTHENTICATION_FAILED = "authentication_failed"
    INCORRECT_CVV = "incorrect_cvv"
    PAYMENT_RISK_CHECK_FAILED = "payment_risk_check_failed"
    TRANSACTION_LIMIT_EXCEEDED = "transaction_limit_exceeded"
    GATEWAY_TECHNICAL_ERROR = "gateway_technical_error"
    BANK_TECHNICAL_ERROR = "bank_technical_error"
    PAYMENT_TIMED_OUT = "payment_timed_out"
    PAYMENT_CANCELLED = "payment_cancelled"

    # UPI / NPCI codes without a card equivalent
    VELOCITY_EXCEEDED = "velocity_exceeded"  # NPCI Z7
    PER_TXN_LIMIT_EXCEEDED = "per_txn_limit_exceeded"  # NPCI Z8
    COLLECT_EXPIRED = "collect_expired"  # NPCI U69
    DEBIT_FAILED = "debit_failed"  # NPCI U30
    REMITTER_BANK_DOWN = "remitter_bank_down"  # NPCI U28

    # Mandate
    MANDATE_REVOKED = "mandate_revoked"
    MANDATE_PAUSED = "mandate_paused"
    AFA_REQUIRED = "afa_required"

    # Terminal
    UNKNOWN = "unknown"


class FailureSource(StrEnum):
    """https://razorpay.com/docs/errors/ — the `source` field on Razorpay's error object."""

    CUSTOMER = "customer"
    BUSINESS = "business"
    RAZORPAY = "razorpay"
    GATEWAY = "gateway"
    BANK = "bank"
    NETWORK = "network"


class Severity(StrEnum):
    """https://razorpay.com/docs/api/payments/downtime/entity — downtime event `severity`."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Rail(StrEnum):
    UPI_AUTOPAY = "upi_autopay"
    ENACH = "enach"
    CARD = "card"


class MandateState(StrEnum):
    """BUILD_DOC.md §3.1 — sim/world.yaml customer.mandate.state."""

    ACTIVE = "active"
    PAUSED = "paused"
    REVOKED = "revoked"


class ActionType(StrEnum):
    SILENT_RETRY = "silent_retry"
    PRE_DEBIT_NOTICE = "pre_debit_notice"
    CONTACT_LINK = "contact_link"
    CREDENTIAL_UPDATE_REQUEST = "credential_update_request"
    STOP = "stop"


class InvoiceCategory(StrEnum):
    """Drives R002_AFA_THRESHOLD's elevated ceiling — BUILD_DOC.md §1.3."""

    STANDARD = "standard"
    INSURANCE = "insurance"
    MUTUAL_FUND = "mutual_fund"
    CREDIT_CARD_BILL = "credit_card_bill"


class StopRule(StrEnum):
    """BUILD_DOC.md §4.4."""

    HARD_DECLINE = "hard_decline"
    MAX_ATTEMPTS = "max_attempts"
    WINDOW_EXPIRED = "window_expired"
    NEGATIVE_EV = "negative_ev"
    MANDATE_REVOKED = "mandate_revoked"
    CUSTOMER_OPTED_OUT = "customer_opted_out"
    PROMISE_TO_PAY = "promise_to_pay"
    INVOICE_PAID = "invoice_paid"


class DowntimeWindow(FrozenModel):
    """https://razorpay.com/docs/api/payments/downtime/entity — payment.downtime.* shape.

    Not sim-specific: sim/world.py generates these synthetically, execute/razorpay_client.py
    (Phase 9) will read real ones from the same API. policy/downtime.py consumes either
    without knowing which. Lives here, not in sim/, so policy/ never has to import sim/.
    """

    issuer: str
    method: Rail
    severity: Severity
    begin: AwareDatetime
    end: AwareDatetime
    scheduled: bool = False


class FailureEvent(FrozenModel):
    """The raw diagnose input — Razorpay's error object plus which invoice it happened on."""

    invoice_id: str
    code: str
    description: str
    source: FailureSource
    step: str
    reason: str | None = None
    occurred_at: AwareDatetime


class CustomerProfile(FrozenModel):
    customer_id: str
    split: Literal["A", "B"]  # assigned once at cohort generation — BUILD_PLAN.md Phase 3
    language: Literal["en", "hi", "hinglish"]
    mandate_rail: Rail
    mandate_state: MandateState
    mandate_max_amount: Money
    issuer: str
    opted_out: bool = False
    promise_to_pay_until: AwareDatetime | None = None


class Invoice(FrozenModel):
    invoice_id: str
    customer_id: str
    amount: Money
    category: InvoiceCategory
    first_failed_at: AwareDatetime


class Attempt(FrozenModel):
    invoice_id: str
    attempt_index: int
    action_type: ActionType
    rail: Rail
    amount: Money
    notify_at: AwareDatetime | None
    debit_at: AwareDatetime | None

    @property
    def idempotency_key(self) -> str:
        # Invariant 6: deterministic from (invoice_id, attempt_index, action_type). Retries reuse it.
        return f"{self.invoice_id}:{self.attempt_index}:{self.action_type.value}"


class RecoveryPlan(FrozenModel):
    invoice_id: str
    attempts: tuple[Attempt, ...]
    stop_rule: StopRule | None = None


class RuleResult(FrozenModel):
    rule_id: str
    passed: bool
    reason: str | None = None


class ComplianceVerdict(FrozenModel):
    """Every rule's outcome, not just the first failure — the audit trail wants the full picture."""

    approved: bool
    results: tuple[RuleResult, ...]


class Decision(FrozenModel):
    """One append-only audit row, written before the action executes — invariant 7."""

    invoice_id: str
    attempt_index: int
    decided_at: AwareDatetime
    input_snapshot_hash: str
    policy_version: str
    compliance_verdict: ComplianceVerdict
    chosen_action: ActionType
    expected_value: Money
