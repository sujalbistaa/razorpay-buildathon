"""Ablation — BUILD_DOC.md §8: "reason-awareness alone -> + payday inference -> + downtime
gating -> + EV-based stopping -> + dunning," a stacked bar showing why the number moved.

Every stage runs over cohort B only, using the hazard model trained on cohort A -- the same
held-out discipline as the headline learned-vs-razorpay_default comparison in scripts/bench.py,
so no bar in this chart benefits from having seen its own evaluation invoices during training.

Stages 1-3 are restricted HeuristicPolicy action tables (every SILENT_RETRY_* branch collapsed
to the generic RETRY_T3_THEN_CONTACT fallback except the capability being added at that
stage); stage 4 is LearnedPolicy's EV planner with its trailing dunning message removed;
stage 5 is LearnedPolicy exactly as scripts/bench.py's `learned` arm uses it.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vasool.bench.harness import run_arm
from vasool.bench.metrics import compute_metrics
from vasool.domain.types import ActionType, FailureClass, Invoice, RecoveryPlan
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.base import Policy, PolicyContext
from vasool.policy.hazard import HazardModel
from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction, HeuristicPolicy
from vasool.policy.learned import LearnedPolicy
from vasool.policy.planner import PlanningCosts
from vasool.sim.cohort import Cohort

DOWNTIME_CLASSES: tuple[FailureClass, ...] = (
    FailureClass.GATEWAY_TECHNICAL_ERROR,
    FailureClass.REMITTER_BANK_DOWN,
    FailureClass.BANK_TECHNICAL_ERROR,
)

# The three timing branches that encode a specific capability -- every one of them collapses
# to the generic fallback in the "reason-awareness alone" stage, then gets restored one at a
# time. CREDENTIAL_UPDATE_THEN_STOP / STOP_NEVER_RETRY / CONTACT_IMMEDIATELY are left alone at
# every stage: they're structurally forced by the failure class (hard decline, no-retry risk
# check, or a rail-agnostic contact), not a timing choice any of the four capabilities affect.
_TIMED_RETRY_BRANCHES: frozenset[HeuristicAction] = frozenset(
    {
        HeuristicAction.SILENT_RETRY_AT_PAYDAY,
        HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED,
        HeuristicAction.SILENT_RETRY_NEXT_DAY,
    }
)


def _restricted_table(*, payday: bool, downtime: bool) -> dict[FailureClass, HeuristicAction]:
    table = dict(ACTION_TABLE)
    for failure_class, action in ACTION_TABLE.items():
        if action in _TIMED_RETRY_BRANCHES:
            table[failure_class] = HeuristicAction.RETRY_T3_THEN_CONTACT
    if payday:
        table[FailureClass.INSUFFICIENT_FUNDS] = HeuristicAction.SILENT_RETRY_AT_PAYDAY
    if downtime:
        for failure_class in DOWNTIME_CLASSES:
            table[failure_class] = HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED
    return table


class _EVWithoutDunning:
    """Stage 4: LearnedPolicy's EV-based timing with its trailing CONTACT_LINK stripped back
    off -- isolates what EV-based stopping is worth before dunning is added back in stage 5.
    """

    def __init__(self, hazard: HazardModel, costs: PlanningCosts) -> None:
        self._inner = LearnedPolicy(hazard=hazard, costs=costs)

    def plan(self, invoice: Invoice, context: PolicyContext) -> RecoveryPlan:
        plan = self._inner.plan(invoice, context)
        if plan.attempts and plan.attempts[-1].action_type is ActionType.CONTACT_LINK:
            return RecoveryPlan(invoice_id=plan.invoice_id, attempts=plan.attempts[:-1], stop_rule=plan.stop_rule)
        return plan


@dataclass(frozen=True)
class AblationStage:
    label: str
    total_recovered_paise: int


def run_ablation(cohort: Cohort, hazard: HazardModel, costs: PlanningCosts, audit_dir: Path) -> list[AblationStage]:
    b_invoices = tuple(inv for inv in cohort.invoices if cohort.world.customer(inv.customer_id).split == "B")
    cohort_b = dataclasses.replace(cohort, invoices=b_invoices)

    stages: list[tuple[str, Policy]] = [
        ("reason-awareness", HeuristicPolicy(_restricted_table(payday=False, downtime=False))),
        ("+ payday", HeuristicPolicy(_restricted_table(payday=True, downtime=False))),
        ("+ downtime gating", HeuristicPolicy(_restricted_table(payday=True, downtime=True))),
        ("+ EV stopping", _EVWithoutDunning(hazard, costs)),
        ("+ dunning", LearnedPolicy(hazard=hazard, costs=costs)),
    ]
    results = []
    for name, policy in stages:
        executor = SimulatorClient(cohort.world)
        slug = name.replace(" ", "_").replace("+", "plus")
        db_path = str(audit_dir / f"audit_ablation_{slug}.db")
        Path(db_path).unlink(missing_ok=True)
        run_results = run_arm(policy, f"ablation:{name}", cohort_b, executor, db_path)
        metrics = compute_metrics(name, run_results)
        results.append(AblationStage(label=name, total_recovered_paise=metrics.total_recovered.paise))
    return results


def plot_ablation(stages: list[AblationStage], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4.5))
    labels = [s.label for s in stages]
    values = [s.total_recovered_paise / 100 for s in stages]
    ax.bar(labels, values, color="#2a6f97")
    ax.set_ylabel("Rupees recovered (cohort B)")
    ax.set_title("Ablation: contribution of each capability (BUILD_DOC.md §8)")
    ax.tick_params(axis="x", rotation=20)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
