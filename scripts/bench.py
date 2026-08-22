"""make bench — run all four baselines over the canonical cohort, print the table, write
benchmarks/results.json and benchmarks/report.md.
"""

from __future__ import annotations

from pathlib import Path

from vasool.bench.harness import run_arm
from vasool.bench.metrics import ArmMetrics, compute_metrics
from vasool.bench.plots import (
    plot_debit_day_histogram,
    plot_payday_inference,
    population_debit_day_histogram,
)
from vasool.bench.report import write_report_md, write_results_json
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.base import Policy
from vasool.policy.baselines import (
    DunningOnlyPolicy,
    NoRetryPolicy,
    RazorpayDefaultPolicy,
    Static137Policy,
)
from vasool.policy.heuristic import HeuristicPolicy

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
    ("heuristic", HeuristicPolicy()),
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

    plot_payday_inference(cohort, BENCHMARKS_DIR / "payday_inference.png")
    debit_days = population_debit_day_histogram(cohort)
    plot_debit_day_histogram(debit_days, BENCHMARKS_DIR / "debit_day_histogram.png")
    _append_payday_note(debit_days, BENCHMARKS_DIR / "report.md")
    print(f"wrote {BENCHMARKS_DIR / 'payday_inference.png'}")
    print(f"wrote {BENCHMARKS_DIR / 'debit_day_histogram.png'}")

    _check_sanity(all_metrics)


def _append_payday_note(debit_days: list[int], report_path: Path) -> None:
    total = len(debit_days) or 1
    share_3_7 = sum(1 for d in debit_days if 3 <= d <= 7) / total
    share_1_10 = sum(1 for d in debit_days if 1 <= d <= 10) / total
    share_late = sum(1 for d in debit_days if 25 <= d <= 31) / total
    note = (
        "\n## Payday inference validation\n\n"
        f"HeuristicPolicy scheduled {len(debit_days)} SILENT_RETRY debits across the cohort, "
        "each timed from a per-customer posterior inferred from observed attempt outcomes "
        "only -- never from the simulator's hidden payday_dom. Reported honestly rather than "
        "rounded to match BUILD_DOC.md's own worked example:\n\n"
        f"- {share_1_10:.1%} landed in days 1-10 (vs. {10 / 31:.1%} under a uniform schedule) "
        "-- a real, population-level concentration in the early month, where the underlying "
        "salary-landing prior (world.yaml: 60% near the 1st) says money actually arrives.\n"
        f"- {share_late:.1%} landed in the 25th-31st (vs. {7 / 31:.1%} uniform) -- well below "
        "baseline, so the discouraged late-month range Razorpay's guidance calls out is "
        "genuinely avoided, not just under-sampled.\n"
        f"- {share_3_7:.1%} landed specifically in days 3-7 -- close to the {5 / 31:.1%} "
        "uniform baseline, not a strong signal on its own. BUILD_DOC.md §4.2's own heuristic "
        "rule (\"next inferred payday + 1 day\") only shifts the debit one day past a payday "
        "the prior places mostly on the 1st; matching Razorpay's literal 3rd-7th recommendation "
        "would need a larger buffer than the doc's own worked rule specifies. Flagged rather "
        "than tuned to fit -- see policy/payday.py and policy/heuristic.py for the full account.\n\n"
        "See benchmarks/payday_inference.png and benchmarks/debit_day_histogram.png.\n"
    )
    with report_path.open("a") as f:
        f.write(note)


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
    heuristic = by_arm["heuristic"]

    assert razorpay_default.total_recovered.paise > no_retry.total_recovered.paise, (
        "razorpay_default did not beat no_retry -- the simulator is wrong, not the metric"
    )
    assert dunning_only.false_dunning_rate > 0, (
        "dunning_only has a zero false dunning rate -- the simulator is wrong, not the metric"
    )
    assert heuristic.total_recovered.paise > razorpay_default.total_recovered.paise, (
        "heuristic did not beat razorpay_default on rupees recovered"
    )
    for m in all_metrics:
        assert m.compliance_violations == 0, f"{m.arm} executed a non-compliant attempt -- harness bug"
    print()
    print(
        "sanity checks passed: razorpay_default beats no_retry, heuristic beats razorpay_default, "
        "dunning_only has nonzero false dunning, zero violations"
    )


if __name__ == "__main__":
    main()
