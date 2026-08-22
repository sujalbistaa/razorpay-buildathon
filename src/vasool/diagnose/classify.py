"""Composes the two-stage classifier -- same shape as compliance/guard.py composing
compliance/rules.py: the package's single entry point, so a caller never picks between the
rules table and the LLM fallback itself.
"""

from __future__ import annotations

from vasool.diagnose import llm_fallback, rules
from vasool.domain.types import FailureClass, FailureEvent
from vasool.llm.client import LLMClient


def classify_failure(event: FailureEvent, client: LLMClient) -> FailureClass:
    rule_match = rules.classify(event)
    if rule_match is not None:
        return rule_match
    return llm_fallback.classify(event, client)
