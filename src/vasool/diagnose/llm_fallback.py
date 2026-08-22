"""Only reached when rules.py returns None -- an error string the deterministic table doesn't
recognize. Output is constrained to the FailureClass enum plus a confidence score and an
evidence span (CLAUDE.md invariant 2: "classifies unmapped error strings," never invents a
class outside the taxonomy). Below CONFIDENCE_THRESHOLD, or on any LLMClient fallback, the
result is UNKNOWN -- which taxonomy.py already routes to the AMBIGUOUS-soft policy, so a
low-confidence guess degrades to "try once, then contact," never to a wrong confident action.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vasool.domain.types import FailureClass, FailureEvent
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)

# POLICY PARAMETER, not sourced -- how much confidence a below-threshold guess needs before
# it's trusted over UNKNOWN's safe-default ambiguous-soft treatment.
CONFIDENCE_THRESHOLD = 0.6

SYSTEM_PROMPT = (
    "You classify a failed Indian recurring payment into exactly one of a fixed set of "
    "failure classes, from the raw error code/description/reason a payment gateway returned. "
    "Only choose a class from the enum provided. If the error text doesn't clearly match any "
    "class, choose UNKNOWN and say so in your evidence rather than guessing."
)


class ClassificationResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    failure_class: FailureClass
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: str


def _user_prompt(event: FailureEvent) -> str:
    return (
        f"code: {event.code}\n"
        f"description: {event.description}\n"
        f"reason: {event.reason or '(none)'}\n"
        f"source: {event.source.value}\n"
        f"step: {event.step}\n\n"
        f"Valid failure classes: {', '.join(fc.value for fc in FailureClass)}"
    )


def classify(event: FailureEvent, client: LLMClient) -> FailureClass:
    result = client.parse(system=SYSTEM_PROMPT, user=_user_prompt(event), output_format=ClassificationResult)

    if isinstance(result, FallbackTriggered):
        logger.warning("diagnose_llm_fallback", reason=result.reason, event_code=event.code)
        return FailureClass.UNKNOWN

    if result.confidence < CONFIDENCE_THRESHOLD:
        logger.info(
            "diagnose_llm_below_confidence_threshold",
            confidence=result.confidence, proposed_class=result.failure_class.value, event_code=event.code,
        )
        return FailureClass.UNKNOWN

    return result.failure_class
