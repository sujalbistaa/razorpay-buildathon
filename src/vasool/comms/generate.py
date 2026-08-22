"""Recovery message generation — BUILD_DOC.md §6.3: English/Hindi/Hinglish, tone calibrated
to failure class by an LLM completion, always run through validate.py before being trusted.
A rejection (or an LLM fallback) never blocks the send -- it falls back to templates.py's
deterministic copy and logs `llm_validation_failed`, exactly per CLAUDE.md's fallback rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from vasool.comms.templates import OPT_OUT_PHRASE, Language, render
from vasool.comms.validate import MessageValidationResult, validate_message
from vasool.domain.money import Money
from vasool.domain.types import ActionType, FailureClass
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)

_LANGUAGE_NAME: dict[Language, str] = {"en": "English", "hi": "Hindi", "hinglish": "Hinglish (romanized Hindi-English mix)"}

SYSTEM_PROMPT = (
    "You write a short recovery message to an Indian customer whose recurring payment "
    "failed, in the requested language. Calibrate tone to the failure reason -- reassuring "
    "and collaborative for a money-timing issue, direct and action-oriented when the "
    "customer needs to act themselves. Never invent a date, amount, or consequence beyond "
    "what's given. Always include the exact merchant name and amount verbatim, the exact "
    "date given, and end with the exact opt-out line given. No threats, no legal language."
)


@dataclass(frozen=True)
class GeneratedMessage:
    text: str
    source: str  # "llm" or "template" -- surfaced for the audit trail / dashboard degraded badge


def _user_prompt(
    action_type: ActionType, failure_class: FailureClass, language: Language, *,
    merchant_name: str, amount: Money, date_text: str, opt_out_phrase: str,
) -> str:
    return (
        f"Write in {_LANGUAGE_NAME[language]}.\n"
        f"Message type: {action_type.value}\n"
        f"Failure reason: {failure_class.value}\n"
        f"Merchant name (must appear verbatim): {merchant_name}\n"
        f"Amount (must appear verbatim): {amount.format_inr()}\n"
        f"Date (must appear verbatim): {date_text}\n"
        f"Opt-out line (must appear verbatim, at the end): {opt_out_phrase}"
    )


def generate_message(
    action_type: ActionType, failure_class: FailureClass, language: Language, *,
    merchant_name: str, amount: Money, debit_date: date, client: LLMClient, channel: str = "sms",
) -> GeneratedMessage:
    date_text = debit_date.strftime("%d %b")
    template_text = render(
        action_type, failure_class, language, merchant_name=merchant_name, amount=amount, date_text=date_text
    )

    result = client.complete(
        system=SYSTEM_PROMPT,
        user=_user_prompt(
            action_type, failure_class, language, merchant_name=merchant_name, amount=amount,
            date_text=date_text, opt_out_phrase=OPT_OUT_PHRASE[language],
        ),
    )
    if isinstance(result, FallbackTriggered):
        logger.info("comms_llm_fallback", reason=result.reason, failure_class=failure_class.value)
        return GeneratedMessage(text=template_text, source="template")

    verdict: MessageValidationResult = validate_message(
        result, merchant_name=merchant_name, amount=amount, debit_date=debit_date, channel=channel
    )
    if not verdict.approved:
        logger.warning("llm_validation_failed", reasons=verdict.reasons, failure_class=failure_class.value)
        return GeneratedMessage(text=template_text, source="template")

    return GeneratedMessage(text=result, source="llm")
