"""FailureClass -> recoverability and default source. DEFAULT_SOURCE drives the false-dunning metric.

Every entry is marked SOURCED (a specific claim in BUILD_DOC.md, itself URL-cited) or ESTIMATED
(reasoned from the class's own definition — Razorpay does not publish a class-to-source table).
Same honesty convention as sim/world.yaml (BUILD_DOC.md §3.3): do not blur the two.
"""

from __future__ import annotations

from enum import StrEnum

from vasool.domain.types import FailureClass, FailureSource


class Recoverability(StrEnum):
    SOFT = "soft"
    AMBIGUOUS = "ambiguous"
    HARD = "hard"


RECOVERABILITY: dict[FailureClass, Recoverability] = {
    # SOURCED — BUILD_DOC.md §1.2's worked examples of recovery-curve shape.
    FailureClass.INSUFFICIENT_FUNDS: Recoverability.SOFT,  # "recovers when money lands. Payday-shaped."
    FailureClass.REMITTER_BANK_DOWN: Recoverability.SOFT,  # "recovers in minutes to hours. Downtime-shaped."
    FailureClass.GATEWAY_TECHNICAL_ERROR: Recoverability.SOFT,  # same downtime-shaped family as above
    FailureClass.BANK_TECHNICAL_ERROR: Recoverability.SOFT,  # same downtime-shaped family as above
    FailureClass.VELOCITY_EXCEEDED: Recoverability.SOFT,  # "recovers when the velocity window resets."
    FailureClass.PER_TXN_LIMIT_EXCEEDED: Recoverability.SOFT,  # same velocity-window family as Z7
    FailureClass.CARD_EXPIRED: Recoverability.HARD,  # "needs a credential change, i.e. a human."
    FailureClass.DEBIT_INSTRUMENT_BLOCKED: Recoverability.HARD,  # same credential-change family
    FailureClass.MANDATE_REVOKED: Recoverability.HARD,  # same credential-change family
    FailureClass.PAYMENT_RISK_CHECK_FAILED: Recoverability.HARD,  # "must not be retried at all."
    # ESTIMATED — not named in BUILD_DOC.md; reasoned from the class's own definition.
    FailureClass.TRANSACTION_LIMIT_EXCEEDED: Recoverability.SOFT,  # resets next billing/limit cycle
    FailureClass.COLLECT_EXPIRED: Recoverability.SOFT,  # a fresh collect request can simply be sent again
    FailureClass.DEBIT_FAILED: Recoverability.SOFT,  # generic UPI debit failure, no instrument change implied
    FailureClass.PAYMENT_CANCELLED: Recoverability.SOFT,  # customer backed out mid-flow, not a hard block
    FailureClass.CARD_DECLINED: Recoverability.SOFT,  # generic decline, no evidence of a hard block
    FailureClass.AFA_REQUIRED: Recoverability.SOFT,  # resolvable by presenting AFA on the next attempt
    FailureClass.PAYMENT_TIMED_OUT: Recoverability.SOFT,  # transient, network-shaped like a timeout
    FailureClass.AUTHENTICATION_FAILED: Recoverability.AMBIGUOUS,  # needs the customer to retry auth
    FailureClass.INCORRECT_CVV: Recoverability.AMBIGUOUS,  # needs the customer to re-enter correctly
    FailureClass.CARD_NOT_ENROLLED: Recoverability.AMBIGUOUS,  # needs the customer to enroll for 3DS/OTP
    FailureClass.DEBIT_INSTRUMENT_INACTIVE: Recoverability.AMBIGUOUS,  # needs the customer to activate it
    FailureClass.MANDATE_PAUSED: Recoverability.AMBIGUOUS,  # customer-controlled state, can be resumed
    FailureClass.UNKNOWN: Recoverability.AMBIGUOUS,  # BUILD_DOC.md §4.2: "treat as ambiguous soft"
}

HARD_DECLINE_CLASSES: frozenset[FailureClass] = frozenset(
    fc for fc, recoverability in RECOVERABILITY.items() if recoverability is Recoverability.HARD
)

DEFAULT_SOURCE: dict[FailureClass, FailureSource] = {
    # SOURCED — BUILD_DOC.md §1.5: "it isn't their card, it's the bank."
    FailureClass.REMITTER_BANK_DOWN: FailureSource.BANK,
    # ESTIMATED — reasoned from each class's own definition; Razorpay does not publish this mapping.
    FailureClass.INSUFFICIENT_FUNDS: FailureSource.CUSTOMER,
    FailureClass.CARD_EXPIRED: FailureSource.CUSTOMER,
    FailureClass.CARD_DECLINED: FailureSource.BANK,
    FailureClass.DEBIT_INSTRUMENT_BLOCKED: FailureSource.BANK,
    FailureClass.DEBIT_INSTRUMENT_INACTIVE: FailureSource.CUSTOMER,
    FailureClass.CARD_NOT_ENROLLED: FailureSource.CUSTOMER,
    FailureClass.AUTHENTICATION_FAILED: FailureSource.CUSTOMER,
    FailureClass.INCORRECT_CVV: FailureSource.CUSTOMER,
    FailureClass.PAYMENT_RISK_CHECK_FAILED: FailureSource.RAZORPAY,  # Razorpay's own risk engine
    FailureClass.TRANSACTION_LIMIT_EXCEEDED: FailureSource.BANK,  # bank-set limit
    FailureClass.GATEWAY_TECHNICAL_ERROR: FailureSource.GATEWAY,
    FailureClass.BANK_TECHNICAL_ERROR: FailureSource.BANK,
    FailureClass.PAYMENT_TIMED_OUT: FailureSource.NETWORK,
    FailureClass.PAYMENT_CANCELLED: FailureSource.CUSTOMER,
    FailureClass.VELOCITY_EXCEEDED: FailureSource.BANK,  # customer's bank velocity limit
    FailureClass.PER_TXN_LIMIT_EXCEEDED: FailureSource.BANK,
    FailureClass.COLLECT_EXPIRED: FailureSource.CUSTOMER,  # customer didn't approve in time
    FailureClass.DEBIT_FAILED: FailureSource.BANK,
    FailureClass.MANDATE_REVOKED: FailureSource.CUSTOMER,  # revoked via their own bank app
    FailureClass.MANDATE_PAUSED: FailureSource.CUSTOMER,
    FailureClass.AFA_REQUIRED: FailureSource.RAZORPAY,  # regulatory step the platform enforces
    FailureClass.UNKNOWN: FailureSource.CUSTOMER,  # no signal to suppress messaging; matches heuristic default
}
