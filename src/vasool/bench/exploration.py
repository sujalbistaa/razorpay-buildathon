"""Generates the SILENT_RETRY exploration log hazard.py trains on — BUILD_DOC.md §4.3:
"trained on an exploration log generated from cohort A." Thompson sampling (policy/explore.py)
picks which candidate slot to probe once a (failure_class, time_bucket) cell has enough
evidence to have an opinion, so the log accumulates real variety instead of only what a
deterministic policy would already pick — the "keeps learning without spending real money on
bad arms" property BUILD_DOC.md §4.3 describes for Thompson sampling.

Every probe queries World.attempt() directly. This is offline training-log generation
against the simulator's ground truth, not a constrained benchmark run — it deliberately isn't
bounded by R003's 4-silent-attempt ceiling the way a real recovery plan would be, because it
needs many more (context, slot) -> outcome rows per invoice than any one plan would ever
generate.
"""

from __future__ import annotations

from datetime import timedelta

import numpy as np

from vasool.compliance.constants import MAX_ATTEMPT_WINDOW_DAYS
from vasool.domain.timezones import ist_date, to_ist
from vasool.domain.types import ActionType, Attempt, FailureClass, Invoice
from vasool.policy.downtime import DowntimeTracker
from vasool.policy.explore import ExploreCell, ThompsonExplorer
from vasool.policy.hazard import HazardExample, HazardFeatures
from vasool.policy.payday import PaydayObservation, PaydayPosterior
from vasool.policy.planner import candidate_debit_times, time_bucket
from vasool.sim.cohort import Cohort

# BUILD_DOC.md §4.3: "a few thousand attempts" -- 3 probes x ~1,000 cohort-A invoices lands
# comfortably in that range without an excessive make bench runtime.
PROBES_PER_INVOICE = 3

# Matches MAX_SILENT_ATTEMPTS - 1 (compliance/constants.py): the log should cover the same
# attempt_index range planner.py will actually query the trained model with.
MAX_SILENT_ATTEMPT_INDEX = 3


def _invoices_by_customer(cohort: Cohort) -> dict[str, list[Invoice]]:
    index: dict[str, list[Invoice]] = {}
    for invoice in cohort.invoices:
        index.setdefault(invoice.customer_id, []).append(invoice)
    return index


def _payday_evidence_for(invoice: Invoice, siblings: list[Invoice], cohort: Cohort) -> tuple[PaydayObservation, ...]:
    return tuple(
        PaydayObservation(
            day_of_month=ist_date(sibling.first_failed_at).day,
            insufficient_funds=cohort.origin_failures[sibling.invoice_id] is FailureClass.INSUFFICIENT_FUNDS,
        )
        for sibling in siblings
        if sibling.invoice_id != invoice.invoice_id
    )


def generate_exploration_log(
    cohort: Cohort, seed: int, probes_per_invoice: int = PROBES_PER_INVOICE
) -> tuple[HazardExample, ...]:
    """Cohort A only (BUILD_DOC.md §8: "split by customer... learned policy trains on cohort
    A's exploration log"). Deterministic in `seed` alone -- invariant 8.
    """
    rng = np.random.default_rng(seed)
    explorer = ThompsonExplorer.empty()
    examples: list[HazardExample] = []
    siblings = _invoices_by_customer(cohort)

    for invoice in cohort.invoices:
        customer = cohort.world.customer(invoice.customer_id)
        if customer.split != "A":
            continue

        failure_class = cohort.origin_failures[invoice.invoice_id]
        payday_evidence = _payday_evidence_for(invoice, siblings[invoice.customer_id], cohort)
        payday_estimate = PaydayPosterior.infer(payday_evidence).map_estimate()
        tracker = DowntimeTracker(cohort.world.downtime_windows_known_by(invoice.first_failed_at))
        window_end = invoice.first_failed_at + timedelta(days=MAX_ATTEMPT_WINDOW_DAYS)
        candidates = candidate_debit_times(invoice.first_failed_at, window_end)
        if not candidates:
            continue

        for _ in range(probes_per_invoice):
            attempt_index = int(rng.integers(0, MAX_SILENT_ATTEMPT_INDEX + 1))
            debit_at = candidates[int(rng.integers(0, len(candidates)))]
            days_since_failure = (debit_at - invoice.first_failed_at).total_seconds() / 86400
            cell = ExploreCell(failure_class, time_bucket(days_since_failure))

            if not explorer.is_uncertain(cell):
                # Exploit: once a cell has enough evidence to have an opinion, prefer
                # whichever candidate the posterior currently favours instead of spending the
                # probe uniformly at random -- still a *sample*, not the posterior mean, so
                # a cell that looked good by chance can still lose favour later.
                scored = [(c, explorer.sample(cell, rng)) for c in candidates]
                debit_at = max(scored, key=lambda pair: pair[1])[0]
                days_since_failure = (debit_at - invoice.first_failed_at).total_seconds() / 86400

            issuer_up = not tracker.is_down(customer.issuer, customer.mandate_rail, debit_at)
            probe = Attempt(
                invoice_id=invoice.invoice_id, attempt_index=attempt_index, action_type=ActionType.SILENT_RETRY,
                rail=customer.mandate_rail, amount=invoice.amount, notify_at=None, debit_at=debit_at,
            )
            outcome = cohort.world.attempt(invoice, probe, debit_at)

            features = HazardFeatures(
                failure_class=failure_class,
                days_since_failure=days_since_failure,
                days_relative_to_payday=float(ist_date(debit_at).day - payday_estimate),
                issuer_up=issuer_up,
                attempt_index=attempt_index,
                amount=invoice.amount,
                rail=customer.mandate_rail,
                hour=to_ist(debit_at).hour,
            )
            examples.append(HazardExample(features=features, success=outcome.success))
            explorer = explorer.update(cell, outcome.success)

    return tuple(examples)
