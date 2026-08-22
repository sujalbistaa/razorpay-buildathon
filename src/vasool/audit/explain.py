"""Structured Decision -> one English sentence. BUILD_DOC.md §6.5: "the structured record is
authoritative; the sentence is derived and never load-bearing" -- nothing downstream reads
this string to decide anything, and the deterministic fallback (used whenever the LLM path is
unavailable) is not a lesser version of the explanation, just a plainer one built from the
same fields the LLM would have been given.
"""

from __future__ import annotations

from vasool.domain.types import Decision
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You explain one automated payment-recovery decision to a merchant support agent, in "
    "one plain English sentence. State what was decided and why, using only the facts given "
    "-- don't speculate about anything not in the record."
)


def _deterministic_sentence(decision: Decision) -> str:
    verdict = "approved" if decision.compliance_verdict.approved else "rejected"
    failed_rules = ", ".join(r.rule_id for r in decision.compliance_verdict.results if not r.passed)
    rule_note = f" (blocked by {failed_rules})" if failed_rules else ""
    return (
        f"Invoice {decision.invoice_id}, attempt {decision.attempt_index}: policy "
        f"{decision.policy_version} chose {decision.chosen_action.value}, compliance "
        f"{verdict}{rule_note}, expected value {decision.expected_value.format_inr()}."
    )


def _user_prompt(decision: Decision) -> str:
    return (
        f"invoice_id: {decision.invoice_id}\n"
        f"attempt_index: {decision.attempt_index}\n"
        f"policy_version: {decision.policy_version}\n"
        f"chosen_action: {decision.chosen_action.value}\n"
        f"expected_value: {decision.expected_value.format_inr()}\n"
        f"compliance_approved: {decision.compliance_verdict.approved}\n"
        f"compliance_results: "
        + "; ".join(f"{r.rule_id}={'pass' if r.passed else 'fail: ' + (r.reason or '')}" for r in decision.compliance_verdict.results)
    )


def explain_decision(decision: Decision, client: LLMClient) -> str:
    result = client.complete(system=SYSTEM_PROMPT, user=_user_prompt(decision), max_tokens=200)
    if isinstance(result, FallbackTriggered):
        logger.info("explain_llm_fallback", reason=result.reason, invoice_id=decision.invoice_id)
        return _deterministic_sentence(decision)
    return result
