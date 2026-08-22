"""BUILD_PLAN.md Phase 7 accept: feed deliberately bad generations -- wrong amount,
hallucinated date, missing opt-out, a threat -- and assert each is rejected and the template
is used. validate.py is tested directly (table-driven), then generate.py is tested against a
fake LLMClient that returns exactly these bad generations, proving the fallback actually
fires end to end.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

import pytest

from vasool.comms.generate import generate_message
from vasool.comms.validate import validate_message
from vasool.domain.money import Money
from vasool.domain.types import ActionType, FailureClass

MERCHANT = "Vasool"
AMOUNT = Money.from_rupees(499)
DEBIT_DATE = date(2026, 6, 20)

GOOD_MESSAGE = "Hi, your payment of ₹499.00 to Vasool didn't go through. We'll try again on 20 Jun. Reply STOP to opt out."


def _validate(text: str) -> bool:
    return validate_message(text, merchant_name=MERCHANT, amount=AMOUNT, debit_date=DEBIT_DATE).approved


def test_good_message_is_approved() -> None:
    assert _validate(GOOD_MESSAGE) is True


@pytest.mark.parametrize(
    ("bad_message", "expected_reason_substring"),
    [
        ("Hi, your payment of ₹450.00 to Vasool didn't go through. We'll retry on 20 Jun. Reply STOP to opt out.", "amount"),
        ("Hi, your payment of ₹499.00 to Vasool didn't go through. We'll retry on 25 Jun. Reply STOP to opt out.", "date"),
        ("Hi, your payment of ₹499.00 to Vasool didn't go through. We'll retry on 20 Jun.", "opt-out"),
        ("Hi, your payment of ₹499.00 to Vasool didn't go through and legal action will follow. We'll retry on 20 Jun. Reply STOP to opt out.", "prohibited"),
        ("Hi, your payment of ₹499.00 didn't go through. We'll retry on 20 Jun. Reply STOP to opt out.", "merchant"),
        ("Pay up or we call the police about your ₹499.00 payment to Vasool, due 20 Jun. Reply STOP to opt out.", "prohibited"),
    ],
    ids=["wrong_amount", "hallucinated_date", "missing_opt_out", "threat_legal_action", "missing_merchant_name", "threat_police"],
)
def test_bad_generation_is_rejected(bad_message: str, expected_reason_substring: str) -> None:
    result = validate_message(bad_message, merchant_name=MERCHANT, amount=AMOUNT, debit_date=DEBIT_DATE)
    assert result.approved is False
    assert any(expected_reason_substring in reason for reason in result.reasons), result.reasons


def test_length_cap_rejects_an_oversized_sms() -> None:
    long_message = GOOD_MESSAGE + " " + ("padding " * 100)
    result = validate_message(long_message, merchant_name=MERCHANT, amount=AMOUNT, debit_date=DEBIT_DATE, channel="sms")
    assert result.approved is False
    assert any("length cap" in reason for reason in result.reasons)


@dataclass(frozen=True)
class _FakeLLMClient:
    """Stands in for LLMClient in generate.py's call site -- returns a fixed bad completion
    instead of ever touching the network, so the fallback-to-template path is provable
    without VASOOL_LLM or a key.
    """

    canned_response: str

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str:
        return self.canned_response


@pytest.mark.parametrize(
    "bad_response",
    [
        "Hi, your payment of ₹1.00 to Vasool didn't go through. We'll retry on 20 Jun. Reply STOP to opt out.",
        "Hi, your payment of ₹499.00 to Vasool didn't go through. We'll retry on 01 Jan. Reply STOP to opt out.",
        "Hi, your payment of ₹499.00 to Vasool didn't go through. We'll retry on 20 Jun.",
        "We will sue you over your ₹499.00 payment to Vasool due 20 Jun. Reply STOP to opt out.",
    ],
    ids=["wrong_amount", "hallucinated_date", "missing_opt_out", "threat"],
)
def test_generate_message_falls_back_to_template_on_bad_llm_output(bad_response: str) -> None:
    fake_client = _FakeLLMClient(canned_response=bad_response)
    message = generate_message(
        ActionType.PRE_DEBIT_NOTICE, FailureClass.INSUFFICIENT_FUNDS, "en",
        merchant_name=MERCHANT, amount=AMOUNT, debit_date=DEBIT_DATE, client=fake_client,  # type: ignore[arg-type]
    )
    assert message.source == "template"
    assert _validate(message.text) is True


def test_generate_message_uses_llm_output_when_it_passes_validation() -> None:
    fake_client = _FakeLLMClient(canned_response=GOOD_MESSAGE)
    message = generate_message(
        ActionType.PRE_DEBIT_NOTICE, FailureClass.INSUFFICIENT_FUNDS, "en",
        merchant_name=MERCHANT, amount=AMOUNT, debit_date=DEBIT_DATE, client=fake_client,  # type: ignore[arg-type]
    )
    assert message.source == "llm"
    assert message.text == GOOD_MESSAGE


@pytest.mark.parametrize("failure_class", list(FailureClass))
def test_every_failure_class_template_passes_its_own_validator(failure_class: FailureClass) -> None:
    # Every class heuristic.py's ACTION_TABLE can route to a message-bearing branch must have
    # a template that is itself compliant -- otherwise the "fallback" would be a second way to
    # fail, not a safety net.
    from vasool.comms.templates import render
    from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction

    action_type_for = {
        HeuristicAction.SILENT_RETRY_AT_PAYDAY: ActionType.PRE_DEBIT_NOTICE,
        HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED: ActionType.PRE_DEBIT_NOTICE,
        HeuristicAction.SILENT_RETRY_NEXT_DAY: ActionType.PRE_DEBIT_NOTICE,
        HeuristicAction.RETRY_T3_THEN_CONTACT: ActionType.PRE_DEBIT_NOTICE,
        HeuristicAction.CONTACT_IMMEDIATELY: ActionType.CONTACT_LINK,
        HeuristicAction.CREDENTIAL_UPDATE_THEN_STOP: ActionType.CREDENTIAL_UPDATE_REQUEST,
    }
    action = ACTION_TABLE[failure_class]
    if action not in action_type_for:
        return  # STOP_NEVER_RETRY sends no message at all
    action_type = action_type_for[action]
    for language in ("en", "hi", "hinglish"):
        text = render(action_type, failure_class, language, merchant_name=MERCHANT, amount=AMOUNT, date_text="20 Jun")
        assert _validate(text) is True, f"{failure_class}/{language}: {text}"
