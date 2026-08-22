"""Every constant here carries a source. Invariant 4: no magic number in compliance/ without one.

HARD_DECLINE_CLASSES lives in domain/taxonomy.py, not here — it's derived from RECOVERABILITY
there and re-imported by R006 rather than duplicated, so the two definitions can't drift.
"""

from __future__ import annotations

from vasool.domain.money import Money
from vasool.domain.types import InvoiceCategory

# RBI Digital Payments – E-mandate Framework, 2026 (Circular RBI/DPSS/2026-27/396, 21 Apr 2026):
# pre-transaction notification at least 24 hours before every recurring debit.
PRE_DEBIT_NOTICE_HOURS: int = 24

# Same circular: AFA-free ceiling for a recurring transaction.
AFA_FREE_LIMIT: Money = Money(15_00_000)

# Same circular: elevated AFA-free ceiling for insurance, mutual funds and credit-card
# bills registered under e-mandate.
AFA_FREE_ELEVATED_LIMIT: Money = Money(1_00_00_000)

# Same circular: the categories the elevated ceiling applies to.
AFA_ELEVATED_CATEGORIES: frozenset[InvoiceCategory] = frozenset(
    {InvoiceCategory.INSURANCE, InvoiceCategory.MUTUAL_FUND, InvoiceCategory.CREDIT_CARD_BILL}
)

# Matches Razorpay Subscriptions' own retry exhaustion point (T+1, T+2, T+3, then halted) —
# https://razorpay.com/docs/payments/subscriptions/payment-retries/
MAX_SILENT_ATTEMPTS: int = 4

# BUILD_DOC.md §4.4 — the bounded recovery window from first failure.
MAX_ATTEMPT_WINDOW_DAYS: int = 14

# Card-network guidance against same-path retry storms. BUILD_DOC.md §1.4, citing Gr4vy,
# 2 June 2026: practical envelope of >=24h between attempts on the same path. Industry
# figure, not a Razorpay-published rule — see ASSUMPTIONS.md.
MIN_HOURS_BETWEEN_SAME_PATH: int = 24

# ESTIMATED — BUILD_DOC.md §5.1 lists this constant but no source; a conventional
# no-contact window bookending the night in Asia/Kolkata.
CONTACT_QUIET_HOURS_IST: tuple[int, int] = (21, 9)

# ESTIMATED — BUILD_DOC.md §5.1: message-frequency cap to bound customer annoyance.
# No regulatory source; a policy choice.
MAX_MESSAGES_PER_INVOICE: int = 3
MIN_HOURS_BETWEEN_MESSAGES: int = 48

# ESTIMATED — no published Razorpay per-issuer retry rate limit. Conservative bound to
# avoid a thundering herd when an issuer returns from downtime (BUILD_DOC.md §5.1, §3.2).
ISSUER_BUCKET_CAPACITY: float = 20.0
ISSUER_BUCKET_REFILL_PER_HOUR: float = 60.0
