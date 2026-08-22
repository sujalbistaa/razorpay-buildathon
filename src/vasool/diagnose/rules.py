"""Deterministic error-string -> FailureClass table. Primary path: the LLM (llm_fallback.py)
is only consulted when this returns None. Pure, no I/O -- everything needed is in the event.

Two independent matches, both against strings already sourced in domain/types.py's
FailureClass docstring rather than invented here:

1. `event.reason` matched exactly against a FailureClass value. Real Razorpay card-error
   payloads carry a granular `reason` field that is often already one of these slugs
   (razorpay.com/docs/errors/payments/cards/); the simulator's FailureEvent.reason always is
   one, by construction (sim/world.py's _fail_outcome).
2. The NPCI short-codes cited in the same docstring (Z7, Z8, Z9, U69, U30, U28) matched as a
   case-insensitive substring of `event.code` or `event.description` -- a real UPI/NPCI
   failure surfaces as one of these short codes at least as often as the descriptive slug.
"""

from __future__ import annotations

import re

from vasool.domain.types import FailureClass, FailureEvent

_BY_REASON: dict[str, FailureClass] = {fc.value: fc for fc in FailureClass}

# domain/types.py's FailureClass docstring: "UPI/NPCI codes without a card equivalent."
_NPCI_SHORT_CODES: dict[str, FailureClass] = {
    "Z9": FailureClass.INSUFFICIENT_FUNDS,
    "Z7": FailureClass.VELOCITY_EXCEEDED,
    "Z8": FailureClass.PER_TXN_LIMIT_EXCEEDED,
    "U69": FailureClass.COLLECT_EXPIRED,
    "U30": FailureClass.DEBIT_FAILED,
    "U28": FailureClass.REMITTER_BANK_DOWN,
}


def classify(event: FailureEvent) -> FailureClass | None:
    if event.reason is not None and event.reason in _BY_REASON:
        return _BY_REASON[event.reason]

    # Word-boundary match, not a bare substring search -- a 2-3 character short code like
    # "Z9" would otherwise false-positive inside an unrelated longer token.
    haystack = f"{event.code} {event.description}".upper()
    for code, failure_class in _NPCI_SHORT_CODES.items():
        if re.search(rf"\b{re.escape(code)}\b", haystack):
            return failure_class

    return None
