"""llm/policy_compiler.py had no coverage before api/policy_demo.py started calling it live
from the dashboard (BUILD_DOC.md §6.4: "the LLM writes a proposal; a human approves a diff").
Table-driven per CLAUDE.md's testing rule: diff_against_default and the confirmed=True gate
are pure and deserve exactly that treatment.
"""

from __future__ import annotations

import httpx
import pytest

from vasool.compliance.constants import MAX_SILENT_ATTEMPTS
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.llm.policy_compiler import (
    PolicyActivationError,
    PolicyRule,
    _CompiledFields,
    activate,
    compile_policy_rule,
    diff_against_default,
)


def test_compile_policy_rule_falls_back_in_stub_mode() -> None:
    result = compile_policy_rule("don't retry anything under Rs.100 more than twice", LLMClient(), rule_id="r1")
    assert isinstance(result, FallbackTriggered)
    assert result.reason == "stub_mode"


@pytest.fixture
def raising_llm_client(monkeypatch: pytest.MonkeyPatch) -> LLMClient:
    monkeypatch.setenv("VASOOL_LLM", "live")
    monkeypatch.setenv("GROQ_API_KEY", "fake-key-for-testing")
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-for-testing")
    client = LLMClient()
    assert client._groq is not None
    assert client._gemini is not None

    def _raise(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("simulated failure")

    monkeypatch.setattr(client._groq, "post", _raise)
    monkeypatch.setattr(client._gemini, "post", _raise)
    return client


def test_compile_policy_rule_refuses_rather_than_guesses_when_llm_unavailable(
    raising_llm_client: LLMClient,
) -> None:
    # Unlike a customer message (which has a deterministic template fallback), there is no
    # synthesized answer for "what did this arbitrary sentence mean as a rule" -- refusing is
    # the correct behavior, and this is the test that would fail if someone "fixed" that by
    # adding a guessing fallback.
    result = compile_policy_rule("waive everything", raising_llm_client, rule_id="r1")
    assert isinstance(result, FallbackTriggered)


def test_compile_policy_rule_builds_a_rule_from_the_llms_structured_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = LLMClient()
    monkeypatch.setattr(
        client, "parse", lambda **kwargs: _CompiledFields(min_amount_paise=10_000, max_silent_attempts=2)
    )
    result = compile_policy_rule("don't retry anything under Rs.100 more than twice", client, rule_id="r1")
    assert isinstance(result, PolicyRule)
    assert result.rule_id == "r1"
    assert result.min_amount_paise == 10_000
    assert result.max_silent_attempts == 2


@pytest.mark.parametrize(
    "min_amount_paise,max_silent_attempts,expected_fields",
    [
        (None, MAX_SILENT_ATTEMPTS, set()),
        (10_000, MAX_SILENT_ATTEMPTS, {"min_amount_paise"}),
        (None, 2, {"max_silent_attempts"}),
        (10_000, 2, {"min_amount_paise", "max_silent_attempts"}),
    ],
)
def test_diff_against_default_reports_only_changed_fields(
    min_amount_paise: int | None, max_silent_attempts: int, expected_fields: set[str]
) -> None:
    rule = PolicyRule(
        rule_id="r1", description="d", min_amount_paise=min_amount_paise, max_silent_attempts=max_silent_attempts
    )
    diffs = diff_against_default(rule)
    assert {d.field for d in diffs} == expected_fields


def test_activate_raises_without_explicit_confirmation() -> None:
    rule = PolicyRule(rule_id="r1", description="d")
    with pytest.raises(PolicyActivationError):
        activate(rule, confirmed=False)


def test_activate_returns_the_rule_when_confirmed() -> None:
    rule = PolicyRule(rule_id="r1", description="d")
    assert activate(rule, confirmed=True) is rule


def test_policy_rule_cannot_propose_above_the_compliance_ceiling() -> None:
    # Structural, not a trust-the-model check: the output schema itself won't accept a value
    # above the platform's own MAX_SILENT_ATTEMPTS, so the LLM can't loosen a compliance
    # constant even if it tried.
    with pytest.raises(ValueError):
        PolicyRule(rule_id="r1", description="d", max_silent_attempts=MAX_SILENT_ATTEMPTS + 1)
