"""Deterministic FailureClass x language templates — what comms/generate.py falls back to
whenever the LLM path is unavailable or its output fails validation. BUILD_DOC.md §6: tone
calibrated to failure class, not "your card was declined" for everyone.

Hindi and Hinglish strings here are basic, illustrative copy — not reviewed by a native
speaker or professional translator. A real deployment would replace these with reviewed copy
before a single real send; they exist so the fallback path is genuinely trilingual rather
than silently defaulting non-English customers to English.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from vasool.domain.money import Money
from vasool.domain.types import ActionType, FailureClass
from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction

Language = Literal["en", "hi", "hinglish"]


class MessageTone(StrEnum):
    REASSURING_RETRY = "reassuring_retry"  # insufficient_funds-shaped: "we'll try again, or pay now"
    GENERIC_RETRY = "generic_retry"  # a soft decline with no specific story to tell yet
    ACTION_NEEDED = "action_needed"  # the customer, not a retry, is what fixes this
    CREDENTIAL_UPDATE = "credential_update"  # the payment method itself needs updating


# Reuses heuristic.py's own routing rather than re-deriving a second FailureClass grouping
# that could quietly drift from it.
_ACTION_TO_TONE: dict[HeuristicAction, MessageTone] = {
    HeuristicAction.SILENT_RETRY_AT_PAYDAY: MessageTone.REASSURING_RETRY,
    HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED: MessageTone.REASSURING_RETRY,
    HeuristicAction.SILENT_RETRY_NEXT_DAY: MessageTone.GENERIC_RETRY,
    HeuristicAction.RETRY_T3_THEN_CONTACT: MessageTone.GENERIC_RETRY,
    HeuristicAction.CONTACT_IMMEDIATELY: MessageTone.ACTION_NEEDED,
    HeuristicAction.STOP_NEVER_RETRY: MessageTone.ACTION_NEEDED,
    HeuristicAction.CREDENTIAL_UPDATE_THEN_STOP: MessageTone.CREDENTIAL_UPDATE,
}


def tone_for(failure_class: FailureClass) -> MessageTone:
    return _ACTION_TO_TONE[ACTION_TABLE[failure_class]]


OPT_OUT_PHRASE: dict[Language, str] = {
    "en": "Reply STOP to opt out.",
    "hi": "Opt out karne ke liye STOP reply karein.",
    "hinglish": "Opt out karna ho toh STOP reply kar dijiye.",
}

# Keyed by (action_type, tone, language) -- only combinations heuristic.py's ACTION_TABLE can
# actually produce exist here (e.g. CREDENTIAL_UPDATE_REQUEST only ever carries the
# CREDENTIAL_UPDATE tone). {merchant_name}/{amount}/{date}/{opt_out} are filled by render().
_TEMPLATES: dict[tuple[ActionType, MessageTone, Language], str] = {
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.REASSURING_RETRY, "en"):
        "Hi, your payment of {amount} to {merchant_name} didn't go through. We'll try again on {date}, or you can pay now. {opt_out}",
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.REASSURING_RETRY, "hi"):
        "Namaste, {merchant_name} ko aapka {amount} ka payment nahi ho paya. Hum {date} ko dobara try karenge, ya aap abhi pay kar sakte hain. {opt_out}",
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.REASSURING_RETRY, "hinglish"):
        "Hi, {merchant_name} ko aapka {amount} ka payment fail ho gaya tha. Hum {date} ko phir try karenge, ya aap abhi pay kar sakte ho. {opt_out}",
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.GENERIC_RETRY, "en"):
        "Hi, your payment of {amount} to {merchant_name} failed. We'll retry on {date}. {opt_out}",
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.GENERIC_RETRY, "hi"):
        "Namaste, {merchant_name} ko {amount} ka payment fail ho gaya. Hum {date} ko phir try karenge. {opt_out}",
    (ActionType.PRE_DEBIT_NOTICE, MessageTone.GENERIC_RETRY, "hinglish"):
        "Hi, {merchant_name} ka {amount} payment fail ho gaya tha, {date} ko phir try karenge. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.ACTION_NEEDED, "en"):
        "Hi, we need you to complete your {amount} payment to {merchant_name} yourself — as of {date}, we can't retry it automatically. Pay here: [link]. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.ACTION_NEEDED, "hi"):
        "Namaste, {merchant_name} ko {amount} ka payment aapko khud complete karna hoga — {date} tak hum ise apne aap retry nahi kar sakte. Yahan pay karein: [link]. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.ACTION_NEEDED, "hinglish"):
        "Hi, {merchant_name} ka {amount} payment aapko khud complete karna padega — {date} tak auto-retry nahi ho sakta. Yahan pay karo: [link]. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.GENERIC_RETRY, "en"):
        "Hi, your payment of {amount} to {merchant_name} is still pending as of {date}. Pay here: [link]. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.GENERIC_RETRY, "hi"):
        "Namaste, {merchant_name} ko {amount} ka payment {date} tak pending hai. Yahan pay karein: [link]. {opt_out}",
    (ActionType.CONTACT_LINK, MessageTone.GENERIC_RETRY, "hinglish"):
        "Hi, {merchant_name} ka {amount} payment {date} tak pending hai. Yahan pay karo: [link]. {opt_out}",
    (ActionType.CREDENTIAL_UPDATE_REQUEST, MessageTone.CREDENTIAL_UPDATE, "en"):
        "Hi, your payment method on file with {merchant_name} needs updating — your {amount} payment couldn't go through as of {date}. Update it here: [link]. {opt_out}",
    (ActionType.CREDENTIAL_UPDATE_REQUEST, MessageTone.CREDENTIAL_UPDATE, "hi"):
        "Namaste, {merchant_name} ke paas aapka payment method update karna hoga — {amount} ka payment {date} tak nahi ho paya. Yahan update karein: [link]. {opt_out}",
    (ActionType.CREDENTIAL_UPDATE_REQUEST, MessageTone.CREDENTIAL_UPDATE, "hinglish"):
        "Hi, {merchant_name} ke paas aapka payment method update karna padega — {amount} ka payment {date} tak fail ho raha hai. Yahan update karo: [link]. {opt_out}",
}


def render(
    action_type: ActionType, failure_class: FailureClass, language: Language, *,
    merchant_name: str, amount: Money, date_text: str,
) -> str:
    tone = tone_for(failure_class)
    key = (action_type, tone, language)
    if key not in _TEMPLATES:
        # Every (action_type, tone) pair heuristic.py's ACTION_TABLE can actually produce has
        # an English template at minimum -- this only triggers for a language this dict
        # hasn't been extended to yet, not a genuinely missing combination.
        key = (action_type, tone, "en")
    template = _TEMPLATES[key]
    return template.format(
        merchant_name=merchant_name, amount=amount.format_inr(), date=date_text, opt_out=OPT_OUT_PHRASE[language]
    )
