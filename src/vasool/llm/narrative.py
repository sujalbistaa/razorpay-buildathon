"""Batch root-cause narrative for the merchant — BUILD_DOC.md §6.2's worked example: "41% of
last Tuesday's failures were HDFC debit issuer downtime between 02:10 and 03:40 IST. Those
customers were messaged by the old schedule and shouldn't have been." Detection (the counts
below) is computed deterministically; only the prose summarizing it goes through the LLM, and
a fallback still returns real numbers, just as a plain report instead of English prose.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from vasool.domain.types import FailureEvent
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You write a short root-cause narrative for a merchant's failed-payment dashboard, from "
    "a breakdown of failure counts by class and source. Call out the largest and most "
    "actionable pattern first. State only what the numbers show -- don't infer a cause the "
    "data doesn't support."
)


@dataclass(frozen=True)
class FailureBreakdown:
    total: int
    by_class: tuple[tuple[str, int], ...]  # (failure_class, count), most common first
    by_source: tuple[tuple[str, int], ...]  # (source, count), most common first


def summarize_events(events: Sequence[FailureEvent]) -> FailureBreakdown:
    class_counts = Counter(e.reason or e.code for e in events)
    source_counts = Counter(e.source.value for e in events)
    return FailureBreakdown(
        total=len(events),
        by_class=tuple(class_counts.most_common()),
        by_source=tuple(source_counts.most_common()),
    )


def _deterministic_report(breakdown: FailureBreakdown) -> str:
    if breakdown.total == 0:
        return "No failures in this window."
    top_class, top_count = breakdown.by_class[0]
    top_source, top_source_count = breakdown.by_source[0]
    return (
        f"{breakdown.total} failures. Largest class: {top_class} "
        f"({top_count}/{breakdown.total}, {100 * top_count / breakdown.total:.0f}%). "
        f"Largest source: {top_source} ({top_source_count}/{breakdown.total}, "
        f"{100 * top_source_count / breakdown.total:.0f}%)."
    )


def _user_prompt(breakdown: FailureBreakdown) -> str:
    classes = ", ".join(f"{name}: {count}" for name, count in breakdown.by_class)
    sources = ", ".join(f"{name}: {count}" for name, count in breakdown.by_source)
    return f"Total failures: {breakdown.total}\nBy failure class: {classes}\nBy source: {sources}"


def generate_narrative(events: Sequence[FailureEvent], client: LLMClient) -> str:
    breakdown = summarize_events(events)
    result = client.complete(system=SYSTEM_PROMPT, user=_user_prompt(breakdown), max_tokens=300)
    if isinstance(result, FallbackTriggered):
        logger.info("narrative_llm_fallback", reason=result.reason, event_count=len(events))
        return _deterministic_report(breakdown)
    return result
