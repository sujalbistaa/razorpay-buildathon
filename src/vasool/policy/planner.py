"""EV-based planner — BUILD_DOC.md §4.3's differentiator: search the compliance-feasible
SILENT_RETRY slots over the next MAX_ATTEMPT_WINDOW_DAYS days, score each by
P(success) x amount - notification_cost - annoyance_cost using the trained HazardModel, and
commit greedily with one step of lookahead. Explicitly not RL (BUILD_DOC.md §4.3): with a
few thousand attempts and eight tabular features, a receding-horizon greedy search does the
job and stays auditable — every candidate's EV can be printed and explained to a compliance
reviewer, a learned RL policy's action can't.

Pure per CLAUDE.md: no I/O, no clock reads. The hazard model, planning costs, payday
estimate and downtime windows are all built by the caller (learned.py) and passed in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from vasool.compliance.constants import (
    MAX_ATTEMPT_WINDOW_DAYS,
    MAX_SILENT_ATTEMPTS,
    MIN_HOURS_BETWEEN_SAME_PATH,
    PRE_DEBIT_NOTICE_HOURS,
)
from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import OutboundMessage, RuleContext
from vasool.domain.money import Money
from vasool.domain.timezones import at_hour_ist, ist_date, to_ist
from vasool.domain.types import ActionType, Attempt, Invoice, RecoveryPlan, StopRule
from vasool.policy.base import PolicyContext
from vasool.policy.downtime import DowntimeTracker
from vasool.policy.explore import ExploreCell, ThompsonExplorer
from vasool.policy.hazard import HazardFeatures, HazardModel

PRE_DEBIT_LEAD = timedelta(hours=PRE_DEBIT_NOTICE_HOURS)

# Modeling choice (BUILD_DOC.md §4.3: "explicitly not RL... greedy with lookahead depth 2"):
# three candidate hours a day keeps the search space small enough to enumerate and explain,
# rather than a minute-level scan a compliance reviewer couldn't audit by eye.
CANDIDATE_HOURS: tuple[int, ...] = (10, 14, 18)

# Bucket width for the Thompson-sampling cell key (policy/explore.py): a week-wide bucket
# keeps the (failure_class x time_bucket) cell count small enough that a ~1,000-invoice
# exploration log actually accumulates evidence per cell.
TIME_BUCKET_DAYS = 7


@dataclass(frozen=True)
class PlanningCosts:
    notification_cost: Money
    annoyance_cost: Money

    @classmethod
    def from_world_config(cls, config: Mapping[str, Any]) -> PlanningCosts:
        costs = config["planning_costs"]
        return cls(
            notification_cost=Money(int(costs["notification_cost_paise"])),
            annoyance_cost=Money(int(costs["annoyance_cost_paise"])),
        )


def ev_paise(p_success: float, amount: Money, costs: PlanningCosts) -> float:
    """EV(a) = P(success | a) x amount - notification_cost - annoyance_cost — BUILD_DOC.md
    §4.3. A plain float, not a Money: this is a ranking score over an uncertain future, never
    an amount that moves (invariant 1 governs real money arithmetic, not a decision
    heuristic). The Decision audit row rounds this to the nearest paise only when writing it
    down, at the boundary where it needs a concrete type.
    """
    return p_success * amount.paise - costs.notification_cost.paise - costs.annoyance_cost.paise


def time_bucket(days_since_failure: float) -> int:
    return int(days_since_failure // TIME_BUCKET_DAYS)


@dataclass(frozen=True)
class Candidate:
    debit_at: datetime
    p_success: float
    ev: float


def candidate_debit_times(now: datetime, window_end: datetime) -> list[datetime]:
    earliest_date = ist_date(now) + timedelta(days=1)
    latest_date = ist_date(window_end)
    times = []
    d = earliest_date
    while d <= latest_date:
        for hour in CANDIDATE_HOURS:
            times.append(at_hour_ist(d, hour))
        d += timedelta(days=1)
    return times


def build_pair(invoice: Invoice, context: PolicyContext, debit_at: datetime, start_index: int) -> tuple[Attempt, Attempt]:
    notify_at = debit_at - PRE_DEBIT_LEAD
    notice = Attempt(
        invoice_id=invoice.invoice_id, attempt_index=start_index, action_type=ActionType.PRE_DEBIT_NOTICE,
        rail=context.customer.mandate_rail, amount=invoice.amount, notify_at=notify_at, debit_at=None,
    )
    debit = Attempt(
        invoice_id=invoice.invoice_id, attempt_index=start_index + 1, action_type=ActionType.SILENT_RETRY,
        rail=context.customer.mandate_rail, amount=invoice.amount, notify_at=None, debit_at=debit_at,
    )
    return notice, debit


def _message_for(invoice: Invoice, notify_at: datetime) -> OutboundMessage:
    return OutboundMessage(
        merchant_name="Revora", amount=invoice.amount, debit_date=ist_date(notify_at), opt_out_included=True
    )


def pair_is_feasible(
    invoice: Invoice, context: PolicyContext, prior_attempts: tuple[Attempt, ...], notice: Attempt, debit: Attempt, issuer_down_at_debit: bool
) -> bool:
    """Mirrors bench/harness.py's incremental evaluation: the notice is checked on its own,
    then the debit is checked *as if* that notice had already been delivered (harness always
    executes attempts in chronological order, so a notice this candidate schedules 24h ahead
    of its own paired debit really will have been delivered by the time execution reaches
    the debit — see harness.py's module docstring for why attempts are evaluated one at a
    time rather than as a whole plan).
    """
    assert notice.notify_at is not None
    notice_context = RuleContext(
        invoice=invoice, customer=context.customer, failure_class=context.failure_class,
        now=notice.notify_at, prior_attempts=prior_attempts, notice_delivered_at=None,
        message=_message_for(invoice, notice.notify_at),
    )
    notice_verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(notice,)), notice_context)
    if not notice_verdict.approved:
        return False

    assert debit.debit_at is not None
    debit_context = RuleContext(
        invoice=invoice, customer=context.customer, failure_class=context.failure_class,
        now=debit.debit_at, prior_attempts=prior_attempts + (notice,), notice_delivered_at=notice.notify_at,
        issuer_down=issuer_down_at_debit,
    )
    debit_verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(debit,)), debit_context)
    return debit_verdict.approved


def score_candidates(
    invoice: Invoice,
    context: PolicyContext,
    prior_attempts: tuple[Attempt, ...],
    silent_attempt_index: int,
    hazard: HazardModel,
    costs: PlanningCosts,
    downtime_tracker: DowntimeTracker,
    payday_map_estimate: int,
    explorer: ThompsonExplorer | None,
    rng: np.random.Generator | None,
) -> list[Candidate]:
    if explorer is not None and rng is None:
        raise ValueError("rng is required whenever an explorer is supplied — invariant 8, no unseeded random")

    window_end = invoice.first_failed_at + timedelta(days=MAX_ATTEMPT_WINDOW_DAYS)
    feasible_times: list[datetime] = []
    feasible_features: list[HazardFeatures] = []
    for debit_at in candidate_debit_times(invoice.first_failed_at, window_end):
        issuer_down = downtime_tracker.is_down(context.customer.issuer, context.customer.mandate_rail, debit_at)
        notice, debit = build_pair(invoice, context, debit_at, start_index=len(prior_attempts))
        if not pair_is_feasible(invoice, context, prior_attempts, notice, debit, issuer_down):
            continue

        feasible_times.append(debit_at)
        feasible_features.append(
            HazardFeatures(
                failure_class=context.failure_class,
                days_since_failure=(debit_at - invoice.first_failed_at).total_seconds() / 86400,
                days_relative_to_payday=float(ist_date(debit_at).day - payday_map_estimate),
                issuer_up=not issuer_down,
                attempt_index=silent_attempt_index,
                amount=invoice.amount,
                rail=context.customer.mandate_rail,
                hour=to_ist(debit_at).hour,
            )
        )

    if not feasible_times:
        return []

    # One batched booster.predict() call over every feasible slot for this step, instead of
    # one call per candidate -- LightGBM inference has enough fixed per-call overhead that a
    # 42-candidate-a-day grid, times up to 4 attempts, times a full cohort, is the difference
    # between make bench finishing in seconds and in minutes.
    probabilities = hazard.predict_many(feasible_features)

    candidates: list[Candidate] = []
    for debit_at, features, p in zip(feasible_times, feasible_features, probabilities, strict=True):
        if explorer is not None:
            assert rng is not None
            cell = ExploreCell(context.failure_class, time_bucket(features.days_since_failure))
            p = explorer.decide(cell, p, rng)
        candidates.append(Candidate(debit_at=debit_at, p_success=p, ev=ev_paise(p, invoice.amount, costs)))
    return candidates


def pick_with_lookahead(candidates: Sequence[Candidate]) -> Candidate | None:
    """Greedy with one step of lookahead (BUILD_DOC.md §4.3): among this step's feasible
    candidates, prefer the one whose own EV plus the best EV still reachable afterwards
    (respecting the same-path spacing rule) is highest — so a slot that's merely OK today but
    forecloses a much better slot tomorrow doesn't win just for being first. The NEGATIVE_EV
    stop decision (in plan_ev_sequence) still checks the *chosen* candidate's own EV, not this
    combined score, so lookahead only breaks ties among currently-positive options.
    """
    if not candidates:
        return None

    def two_step_score(candidate: Candidate) -> float:
        earliest_next = candidate.debit_at + timedelta(hours=MIN_HOURS_BETWEEN_SAME_PATH)
        best_next = max((c.ev for c in candidates if c.debit_at >= earliest_next), default=0.0)
        return candidate.ev + max(0.0, best_next)

    return max(candidates, key=two_step_score)


@dataclass(frozen=True)
class EVPlanResult:
    attempts: tuple[Attempt, ...]
    stop_rule: StopRule | None


def plan_ev_sequence(
    invoice: Invoice,
    context: PolicyContext,
    hazard: HazardModel,
    costs: PlanningCosts,
    downtime_tracker: DowntimeTracker,
    payday_map_estimate: int,
    explorer: ThompsonExplorer | None = None,
    rng: np.random.Generator | None = None,
) -> EVPlanResult:
    """StopRule per BUILD_DOC.md §4.4: NEGATIVE_EV when the best remaining candidate's own EV
    is <= 0, WINDOW_EXPIRED when no compliance-feasible slot remains at all, MAX_ATTEMPTS when
    R003's silent-attempt ceiling is reached with every attempt still EV-positive.
    """
    prior_attempts: list[Attempt] = []
    attempts: list[Attempt] = []
    stop_rule: StopRule | None = None
    silent_count = 0

    while silent_count < MAX_SILENT_ATTEMPTS:
        candidates = score_candidates(
            invoice, context, tuple(prior_attempts), silent_count, hazard, costs, downtime_tracker,
            payday_map_estimate, explorer, rng,
        )
        best = pick_with_lookahead(candidates)
        if best is None:
            stop_rule = StopRule.WINDOW_EXPIRED
            break
        if best.ev <= 0:
            stop_rule = StopRule.NEGATIVE_EV
            break
        notice, debit = build_pair(invoice, context, best.debit_at, start_index=len(attempts))
        attempts.extend((notice, debit))
        prior_attempts.extend((notice, debit))
        silent_count += 1
    else:
        stop_rule = StopRule.MAX_ATTEMPTS

    return EVPlanResult(attempts=tuple(attempts), stop_rule=stop_rule)
