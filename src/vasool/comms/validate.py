"""Mandatory pre-send validator — BUILD_DOC.md §6: "amount, debit date and merchant name must
appear verbatim from the record — string-equality check against canonical values, not a vibe
check." Every LLM-generated message passes through here before comms/generate.py will send
it; a rejection falls back to templates.py and logs `llm_validation_failed` (CLAUDE.md: "fail
-> fall back to the deterministic template, log llm_validation_failed, continue").

The threat/legal-claim/invented-date checks below are keyword and pattern heuristics, not an
NLP classifier — they catch the literal, obvious cases (a demand letter phrase, a legal
citation, a date that isn't the canonical one) and are documented as exactly that rather than
presented as an exhaustive safety net.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from vasool.comms.templates import OPT_OUT_PHRASE
from vasool.domain.money import Money

# ESTIMATED -- no published Razorpay/carrier length limit cited; conventional SMS/WhatsApp/
# email envelopes.
CHANNEL_LENGTH_CAPS: dict[str, int] = {"sms": 320, "whatsapp": 1024, "email": 5000}

# Heuristic, not exhaustive -- BUILD_DOC.md §6: "no threats, no legal claims, no invented
# consequences."
_PROHIBITED_PATTERNS: tuple[str, ...] = (
    r"\blegal action\b", r"\bcourt\b", r"\blawsuit\b", r"\bpolice\b", r"\bcredit score\b",
    r"\bcibil\b", r"\bblacklist", r"\barrest", r"\bsue\b", r"\bpenalty of\b",
)

# Matches DD/MM(/YY[YY]) and DD Month [YYYY] date-like tokens so a generated message can be
# checked for a date other than the canonical one -- not a complete date grammar, but catches
# the formats a template or a compliant LLM completion would actually produce.
_DATE_TOKEN_RE = re.compile(
    r"\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class MessageValidationResult:
    approved: bool
    reasons: tuple[str, ...]


def _canonical_date_strings(debit_date: date) -> set[str]:
    return {
        debit_date.strftime("%d/%m/%Y"), debit_date.strftime("%d-%m-%Y"), debit_date.strftime("%d/%m/%y"),
        debit_date.strftime("%d %B"), debit_date.strftime("%d %b"),
    }


def validate_message(
    text: str, *, merchant_name: str, amount: Money, debit_date: date, channel: str = "sms",
) -> MessageValidationResult:
    reasons: list[str] = []

    if merchant_name not in text:
        reasons.append("merchant name missing")

    if amount.format_inr() not in text:
        reasons.append("exact amount missing")

    canonical_dates = _canonical_date_strings(debit_date)
    found_dates = {m.group(0) for m in _DATE_TOKEN_RE.finditer(text)}
    if found_dates and not found_dates & canonical_dates:
        reasons.append(f"date(s) {sorted(found_dates)} don't match the canonical debit date")
    elif not found_dates:
        reasons.append("debit date missing")

    # Any known language's opt-out phrase counts, not just this message's own language -- an
    # LLM completion that reused a different (still valid) opt-out phrasing shouldn't be
    # rejected for that alone.
    if not any(phrase in text for phrase in OPT_OUT_PHRASE.values()):
        reasons.append("opt-out phrase missing")

    length_cap = CHANNEL_LENGTH_CAPS.get(channel)
    if length_cap is not None and len(text) > length_cap:
        reasons.append(f"exceeds {channel} length cap of {length_cap} characters")

    lowered = text.lower()
    for pattern in _PROHIBITED_PATTERNS:
        if re.search(pattern, lowered):
            reasons.append(f"prohibited content matched pattern: {pattern}")

    return MessageValidationResult(approved=not reasons, reasons=tuple(reasons))
