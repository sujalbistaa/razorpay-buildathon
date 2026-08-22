"""Natural language -> typed PolicyRule -> validate -> diff against live policy -> explicit
confirmation before activation. BUILD_DOC.md §6.4: "The LLM writes a proposal; a human
approves a diff." There is no path from `compile_policy_rule` to `activate` that skips the
`confirmed=True` argument -- `activate` raises without it, and nothing in this module ever
passes it automatically.

Scoped to the two retry-shape knobs BUILD_DOC.md's own worked example exercises ("don't retry
anything under Rs.100 more than twice"): a minimum amount floor and a silent-attempt ceiling.
`max_silent_attempts` is Pydantic-bounded at compliance's own ceiling (MAX_SILENT_ATTEMPTS),
so the LLM cannot even *propose* a value that would loosen a compliance constant -- not a
trust-the-model check, a structural one enforced by the output schema itself.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from vasool.compliance.constants import MAX_SILENT_ATTEMPTS
from vasool.domain.money import Money
from vasool.llm.client import FallbackTriggered, LLMClient
from vasool.logging import get_logger

logger = get_logger(__name__)

SYSTEM_PROMPT = (
    "You compile a merchant's plain-English retry policy instruction into a structured rule. "
    "Only change a field the instruction actually specifies; leave the other at its default "
    "(min_amount_paise: null, max_silent_attempts: the platform ceiling). Never propose "
    "max_silent_attempts above the platform's compliance ceiling."
)


class _CompiledFields(BaseModel):
    model_config = ConfigDict(frozen=True)

    min_amount_paise: int | None = Field(default=None, ge=0)
    max_silent_attempts: int = Field(default=MAX_SILENT_ATTEMPTS, ge=0, le=MAX_SILENT_ATTEMPTS)


class PolicyRule(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_id: str
    description: str
    min_amount_paise: int | None = None
    max_silent_attempts: int = Field(default=MAX_SILENT_ATTEMPTS, ge=0, le=MAX_SILENT_ATTEMPTS)


class PolicyDiff(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    current: str
    proposed: str


class PolicyActivationError(Exception):
    pass


def compile_policy_rule(nl_text: str, client: LLMClient, *, rule_id: str) -> PolicyRule | FallbackTriggered:
    result = client.parse(system=SYSTEM_PROMPT, user=nl_text, output_format=_CompiledFields)
    if isinstance(result, FallbackTriggered):
        logger.info("policy_compiler_llm_fallback", reason=result.reason, rule_id=rule_id)
        return result
    return PolicyRule(
        rule_id=rule_id, description=nl_text,
        min_amount_paise=result.min_amount_paise, max_silent_attempts=result.max_silent_attempts,
    )


def diff_against_default(rule: PolicyRule) -> tuple[PolicyDiff, ...]:
    diffs: list[PolicyDiff] = []
    if rule.max_silent_attempts != MAX_SILENT_ATTEMPTS:
        diffs.append(PolicyDiff(field="max_silent_attempts", current=str(MAX_SILENT_ATTEMPTS), proposed=str(rule.max_silent_attempts)))
    if rule.min_amount_paise is not None:
        diffs.append(PolicyDiff(field="min_amount_paise", current="no floor", proposed=Money(rule.min_amount_paise).format_inr()))
    return tuple(diffs)


def activate(rule: PolicyRule, *, confirmed: bool) -> PolicyRule:
    """Never called with `confirmed=True` anywhere in this codebase -- that argument is for a
    future human-facing confirmation step (Phase 8's admin surface) to pass explicitly, after
    showing the diff from diff_against_default(). Raises otherwise; there is no default path
    to activation.
    """
    if not confirmed:
        raise PolicyActivationError(f"rule {rule.rule_id!r} requires explicit human confirmation before activation")
    logger.info("policy_rule_activated", rule_id=rule.rule_id, diff=[d.model_dump() for d in diff_against_default(rule)])
    return rule
