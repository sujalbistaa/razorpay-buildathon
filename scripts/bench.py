"""make bench — run all four baselines over the canonical cohort, print the table, write
benchmarks/results.json and benchmarks/report.md.
"""

from __future__ import annotations

from pathlib import Path

from vasool.bench.harness import run_arm
from vasool.bench.metrics import ArmMetrics, compute_metrics
from vasool.bench.report import write_report_md, write_results_json
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.base import Policy
from vasool.policy.baselines import (
    DunningOnlyPolicy,
    NoRetryPolicy,
    RazorpayDefaultPolicy,
    Static137Policy,
)

SEED = 42
N_CUSTOMERS = 500
N_INVOICES = 2000
HORIZON_DAYS = 90

REPO_ROOT = Path(__file__).parent.parent
BENCHMARKS_DIR = REPO_ROOT / "benchmarks"

ARMS: list[tuple[str, Policy]] = [
    ("no_retry", NoRetryPolicy()),
    ("razorpay_default", RazorpayDefaultPolicy()),
    ("static_1_3_7", Static137Policy()),
    ("dunning_only", DunningOnlyPolicy()),
]


def main() -> None:
    from vasool.sim.cohort import generate_cohort

    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    print(f"cohort: {len(cohort.customers)} customers, {len(cohort.invoices)} invoices, hash {cohort.content_hash()[:16]}")
    print()

    all_metrics: list[ArmMetrics] = []
    for name, policy in ARMS:
        executor = SimulatorClient(cohort.world)
        db_path = str(BENCHMARKS_DIR / f"audit_{name}.db")
        Path(db_path).unlink(missing_ok=True)
        results = run_arm(policy, policy_version=f"{name}:v1", cohort=cohort, executor=executor, audit_db_path=db_path)
        all_metrics.append(compute_metrics(name, results))

    print_table(all_metrics)

    write_results_json(all_metrics, BENCHMARKS_DIR / "results.json")
    write_report_md(all_metrics, BENCHMARKS_DIR / "report.md")
    print()
    print(f"wrote {BENCHMARKS_DIR / 'results.json'}")
    print(f"wrote {BENCHMARKS_DIR / 'report.md'}")

    _check_sanity(all_metrics)


def print_table(all_metrics: list[ArmMetrics]) -> None:
    header = f"{'arm':<18} {'recovery':>9} {'recovered':>14} {'attempts/rec':>13} {'mean days':>10} {'messages':>9} {'false dun.':>11} {'violations':>11}"
    print(header)
    print("-" * len(header))
    for m in all_metrics:
        mean_days = f"{m.mean_days_to_recovery:.1f}" if m.mean_days_to_recovery is not None else "—"
        print(
            f"{m.arm:<18} {m.recovery_rate:>8.1%} {m.total_recovered.format_inr():>14} "
            f"{m.attempts_per_recovery:>13.2f} {mean_days:>10} {m.messages_sent:>9} "
            f"{m.false_dunning_rate:>10.1%} {m.compliance_violations:>11}"
        )


def _check_sanity(all_metrics: list[ArmMetrics]) -> None:
    by_arm = {m.arm: m for m in all_metrics}
    no_retry = by_arm["no_retry"]
    razorpay_default = by_arm["razorpay_default"]
    dunning_only = by_arm["dunning_only"]

    assert razorpay_default.total_recovered.paise > no_retry.total_recovered.paise, (
        "razorpay_default did not beat no_retry -- the simulator is wrong, not the metric"
    )
    assert dunning_only.false_dunning_rate > 0, (
        "dunning_only has a zero false dunning rate -- the simulator is wrong, not the metric"
    )
    for m in all_metrics:
        assert m.compliance_violations == 0, f"{m.arm} executed a non-compliant attempt -- harness bug"
    print()
    print("sanity checks passed: razorpay_default beats no_retry, dunning_only has nonzero false dunning, zero violations")


if __name__ == "__main__":
    main()
