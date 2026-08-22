"""ComplianceGuard — the hard gate. Approves or rejects; never mutates a plan (CLAUDE.md invariant 3).

Runs every rule against every attempt and collects every result rather than short-circuiting,
because the audit trail (invariant 7) wants the full evaluation, not just the first failure.
The ApprovedPlan | Rejection framing in BUILD_DOC.md §2 is this ComplianceVerdict: `.approved`
is the pass/fail, `.results` carries every rule_id and reason a caller needs to explain a
rejection — a second pair of types would just be the same data under a different name.
"""

from __future__ import annotations

from vasool.compliance.rules import ALL_RULES, RuleContext
from vasool.domain.types import ComplianceVerdict, RecoveryPlan, RuleResult


class ComplianceGuard:
    @staticmethod
    def evaluate(plan: RecoveryPlan, context: RuleContext) -> ComplianceVerdict:
        results: list[RuleResult] = [
            rule(attempt, context) for attempt in plan.attempts for rule in ALL_RULES
        ]
        approved = all(result.passed for result in results)
        return ComplianceVerdict(approved=approved, results=tuple(results))
