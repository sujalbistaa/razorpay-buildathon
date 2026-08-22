"""benchmarks/payday_inference.png — inferred vs true payday (validation only: the true
payday_dom is the simulator's hidden ground truth, pulled here for plotting, never given to
the policy itself — BUILD_PLAN.md Phase 5), plus the population histogram of the debit days
HeuristicPolicy actually chose.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from vasool.domain.timezones import ist_date
from vasool.domain.types import ActionType
from vasool.policy.base import PolicyContext
from vasool.policy.heuristic import HeuristicPolicy
from vasool.policy.payday import PaydayObservation, PaydayPosterior
from vasool.sim.cohort import Cohort

History = dict[str, list[tuple[datetime, bool]]]


def _build_history(cohort: Cohort) -> History:
    history: History = {}
    for invoice in cohort.invoices:
        was_if = cohort.origin_failures[invoice.invoice_id].value == "insufficient_funds"
        history.setdefault(invoice.customer_id, []).append((invoice.first_failed_at, was_if))
    return history


def _evidence(history: History, customer_id: str, exclude: datetime | None = None) -> tuple[PaydayObservation, ...]:
    return tuple(
        PaydayObservation(day_of_month=ist_date(occurred_at).day, insufficient_funds=was_if)
        for occurred_at, was_if in history.get(customer_id, [])
        if occurred_at != exclude
    )


def plot_payday_inference(cohort: Cohort, path: Path) -> None:
    history = _build_history(cohort)

    true_days: list[int] = []
    inferred_days: list[int] = []
    confidences: list[float] = []
    for customer in cohort.customers:
        if len(history.get(customer.customer_id, [])) < 2:
            continue
        posterior = PaydayPosterior.infer(_evidence(history, customer.customer_id))
        true_days.append(customer.payday_dom)
        inferred_days.append(posterior.map_estimate())
        confidences.append(posterior.confidence())

    fig, ax = plt.subplots(figsize=(6, 6))
    scatter = ax.scatter(true_days, inferred_days, c=confidences, cmap="viridis", alpha=0.6, s=20)
    ax.plot([1, 31], [1, 31], color="gray", linestyle="--", linewidth=1, label="perfect inference")
    ax.set_xlabel("true payday_dom (simulator ground truth, hidden from policy)")
    ax.set_ylabel("inferred payday (PaydayPosterior.map_estimate())")
    ax.set_title(f"Payday inference: {len(true_days)} customers with >=2 invoices")
    ax.legend()
    fig.colorbar(scatter, ax=ax, label="posterior confidence")
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)


def population_debit_day_histogram(cohort: Cohort) -> list[int]:
    """Days-of-month HeuristicPolicy actually schedules a SILENT_RETRY debit on, across the
    cohort. BUILD_PLAN.md Phase 5: should concentrate on the 3rd-7th, independently
    reproducing Razorpay's published guidance from data rather than a hardcoded rule.
    """
    policy = HeuristicPolicy()
    history = _build_history(cohort)

    days: list[int] = []
    for invoice in cohort.invoices:
        customer = cohort.world.customer(invoice.customer_id)
        context = PolicyContext(
            customer=customer.to_profile(),
            failure_class=cohort.origin_failures[invoice.invoice_id],
            now=invoice.first_failed_at,
            payday_evidence=_evidence(history, invoice.customer_id, exclude=invoice.first_failed_at),
        )
        plan = policy.plan(invoice, context)
        for attempt in plan.attempts:
            if attempt.action_type is ActionType.SILENT_RETRY and attempt.debit_at is not None:
                days.append(ist_date(attempt.debit_at).day)
    return days


def plot_debit_day_histogram(days: list[int], path: Path) -> None:
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.hist(days, bins=range(1, 33), align="left", rwidth=0.85)
    ax.axvspan(3, 7, alpha=0.15, color="green", label="Razorpay's published guidance: 3rd-7th")
    ax.set_xlabel("day of month")
    ax.set_ylabel("scheduled debits")
    ax.set_title("HeuristicPolicy's chosen debit days, population histogram")
    ax.set_xticks(range(1, 32, 2))
    ax.legend()
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
