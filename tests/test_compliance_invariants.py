"""CLAUDE.md: "runs the full benchmark and asserts zero compliance violations across every
generated attempt. This test is the product." Never mark this xfail, skipped, or loosened --
if it fails, the policy is wrong, not the test.

Runs every arm (baselines, heuristic, and learned against a hazard model trained on this same
small cohort's own cohort-A exploration log) over a cohort sized for pytest's fast-suite
budget rather than scripts/bench.py's full 2,000-invoice production cohort -- CLAUDE.md's
Testing section anticipates exactly this trade-off ("pytest under 30 seconds, or the
benchmark moves behind a marker"). `make bench`'s own `_check_sanity` re-asserts this at full
scale every time it runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from vasool.bench.exploration import generate_exploration_log
from vasool.bench.harness import run_arm
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.base import Policy
from vasool.policy.baselines import (
    DunningOnlyPolicy,
    NoRetryPolicy,
    RazorpayDefaultPolicy,
    Static137Policy,
)
from vasool.policy.hazard import HazardModel
from vasool.policy.heuristic import HeuristicPolicy
from vasool.policy.learned import LearnedPolicy
from vasool.policy.planner import PlanningCosts
from vasool.sim.cohort import generate_cohort
from vasool.sim.world import load_world_config

SEED = 11
N_CUSTOMERS = 120
N_INVOICES = 400
HORIZON_DAYS = 60


def _arms() -> list[tuple[str, Policy]]:
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    log = generate_exploration_log(cohort, seed=SEED)
    hazard = HazardModel.train(log)
    costs = PlanningCosts.from_world_config(load_world_config())
    return [
        ("no_retry", NoRetryPolicy()),
        ("razorpay_default", RazorpayDefaultPolicy()),
        ("static_1_3_7", Static137Policy()),
        ("dunning_only", DunningOnlyPolicy()),
        ("heuristic", HeuristicPolicy()),
        ("learned", LearnedPolicy(hazard=hazard, costs=costs)),
    ]


@pytest.mark.parametrize("name,policy", _arms())
def test_zero_compliance_violations_across_every_attempt(name: str, policy: Policy, tmp_path: Path) -> None:
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    executor = SimulatorClient(cohort.world)
    db_path = str(tmp_path / f"audit_{name}.db")
    results = run_arm(policy, f"{name}:invariant-check", cohort, executor, db_path)

    total_attempts = sum(r.attempts_made for r in results)
    total_violations = sum(r.compliance_violations for r in results)
    assert total_violations == 0, f"{name} executed {total_violations} non-compliant attempt(s) out of {total_attempts}"
