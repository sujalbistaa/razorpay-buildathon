"""bench.harness.run_arm against a small cohort — NoRetryPolicy never touches money, a
real baseline recovers something, and nothing ever executes a compliance-rejected attempt.
"""

import tempfile
from pathlib import Path

from vasool.bench.harness import run_arm
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.baselines import NoRetryPolicy, RazorpayDefaultPolicy
from vasool.sim.cohort import generate_cohort

SEED = 7
N_CUSTOMERS = 50
N_INVOICES = 150
HORIZON_DAYS = 30


def _run(policy):  # type: ignore[no-untyped-def]
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    with tempfile.TemporaryDirectory() as tmp:
        executor = SimulatorClient(cohort.world)
        db_path = str(Path(tmp) / "audit.db")
        return run_arm(policy, "test:v1", cohort, executor, db_path)


def test_no_retry_never_recovers_or_acts() -> None:
    results = _run(NoRetryPolicy())
    assert all(not r.recovered for r in results)
    assert all(r.attempts_made == 0 for r in results)
    assert all(r.messages_sent == 0 for r in results)
    assert all(r.blocked_attempts == 0 for r in results)


def test_razorpay_default_recovers_something_and_never_violates() -> None:
    results = _run(RazorpayDefaultPolicy())
    assert any(r.recovered for r in results)
    assert all(r.compliance_violations == 0 for r in results)
    # every recovery happened through a real charge, so it always carries a positive amount
    for r in results:
        if r.recovered:
            assert r.recovered_paise > 0
            assert r.days_to_recovery is not None
            assert r.days_to_recovery >= 0


def test_razorpay_default_never_recovers_via_pre_debit_notice() -> None:
    # A recovery on day 0 would mean the notice step itself was miscounted as a charge --
    # the earliest real debit in this schedule is T+1.
    results = _run(RazorpayDefaultPolicy())
    for r in results:
        if r.recovered:
            assert r.days_to_recovery >= 1.0
