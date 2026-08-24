"""Seed data for `make up` — BUILD_PLAN.md Phase 8 accept: "dashboard at localhost:8000 with
seeded data." A small cohort, run once through LearnedPolicy (or HeuristicPolicy if no hazard
model can be trained -- CLAUDE.md's own fallback rule), cached on app.state so the dashboard
and inspector have a full reasoning chain to show without waiting for a live webhook.

Deliberately smaller than scripts/bench.py's 500/2,000 production cohort -- this exists to
demo the reasoning chain quickly at server startup, not to produce a benchmark number.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass

from vasool.bench.exploration import generate_exploration_log
from vasool.bench.harness import InvoiceRunResult, run_arm
from vasool.execute.simulator_client import SimulatorClient
from vasool.policy.base import Policy
from vasool.policy.hazard import HazardModel
from vasool.policy.heuristic import HeuristicPolicy
from vasool.policy.learned import LearnedPolicy
from vasool.policy.planner import PlanningCosts
from vasool.sim.cohort import Cohort, generate_cohort
from vasool.sim.world import load_world_config

SEED = 2026
N_CUSTOMERS = 30
N_INVOICES = 80
HORIZON_DAYS = 60


@dataclass(frozen=True)
class SeedData:
    cohort: Cohort
    policy: Policy
    policy_version: str
    results_by_invoice: dict[str, InvoiceRunResult]
    degraded_model: bool
    audit_db_path: str


def build_seed_data() -> SeedData:
    cohort = generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)
    costs = PlanningCosts.from_world_config(load_world_config())

    policy: Policy
    try:
        log = generate_exploration_log(cohort, seed=SEED)
        hazard = HazardModel.train(log)
        policy = LearnedPolicy(hazard, costs)
        policy_version = "learned:seed"
    except ValueError:
        # Too few exploration rows at this cohort size to train -- fall back exactly the way
        # a missing model artefact would in production (CLAUDE.md: "Model file missing ->
        # HeuristicPolicy").
        policy = HeuristicPolicy()
        policy_version = "heuristic:seed"

    executor = SimulatorClient(cohort.world)
    # A real file, not ":memory:" -- api/dashboard.py reads a real row back out of this after
    # the process that wrote it (this function) has returned, which an in-memory sqlite
    # connection wouldn't survive.
    fd, audit_db_path = tempfile.mkstemp(suffix=".db", prefix="vasool_seed_audit_")
    os.close(fd)
    results = run_arm(policy, policy_version, cohort, executor, audit_db_path)
    degraded = isinstance(policy, LearnedPolicy) and policy.degraded

    return SeedData(
        cohort=cohort, policy=policy, policy_version=policy_version,
        results_by_invoice={r.invoice_id: r for r in results}, degraded_model=degraded,
        audit_db_path=audit_db_path,
    )
