"""make seed — generate the canonical cohort and print a plausibility check on the failure mix.

Not a benchmark (that's Phase 4's `make bench`); just proof that `generate_cohort` produces a
reasonable-looking world before anything is built on top of it.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from vasool.domain.taxonomy import RECOVERABILITY
from vasool.domain.types import ActionType, Attempt, FailureClass
from vasool.sim.cohort import generate_cohort

SEED = 42
N_CUSTOMERS = 500
N_INVOICES = 2000
HORIZON_DAYS = 90


def main() -> None:
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)

    print(f"customers: {len(cohort.customers)}")
    print(f"invoices:  {len(cohort.invoices)}")
    print(f"split:     {cohort.split_counts()}")
    print(f"hash:      {cohort.content_hash()}")
    print()

    class_counts: Counter[str] = Counter()
    recoverability_counts: Counter[str] = Counter()
    for invoice in cohort.invoices:
        customer = cohort.world.customer(invoice.customer_id)
        debit_at = invoice.first_failed_at + timedelta(days=1)
        attempt = Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=0,
            action_type=ActionType.SILENT_RETRY,
            rail=customer.mandate_rail,
            amount=invoice.amount,
            notify_at=None,
            debit_at=debit_at,
        )
        outcome = cohort.world.attempt(invoice, attempt, debit_at)
        if outcome.success:
            class_counts["SUCCESS"] += 1
            continue
        assert outcome.failure_event is not None and outcome.failure_event.reason is not None
        failure_class = FailureClass(outcome.failure_event.reason)
        class_counts[failure_class.value] += 1
        recoverability_counts[RECOVERABILITY[failure_class].value] += 1

    total = sum(class_counts.values())
    print(f"first-retry outcome mix (n={total}):")
    for name, count in class_counts.most_common():
        print(f"  {name:28s} {count:5d}  ({100 * count / total:5.1f}%)")

    print()
    print("recoverability mix among failures:")
    failed_total = sum(recoverability_counts.values())
    for name, count in recoverability_counts.most_common():
        print(f"  {name:12s} {count:5d}  ({100 * count / failed_total:5.1f}%)")


if __name__ == "__main__":
    main()
