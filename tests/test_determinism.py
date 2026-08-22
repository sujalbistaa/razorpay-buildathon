"""Same seed, same world.yaml, same results, byte for byte — CLAUDE.md invariant 8."""

from vasool.bench.exploration import generate_exploration_log
from vasool.policy.hazard import HazardModel
from vasool.sim.cohort import generate_cohort

SEED = 42
N_CUSTOMERS = 500
N_INVOICES = 2000
HORIZON_DAYS = 90


def _cohort():  # type: ignore[no-untyped-def]
    return generate_cohort(seed=SEED, n_customers=N_CUSTOMERS, n_invoices=N_INVOICES, horizon_days=HORIZON_DAYS)


def test_two_runs_with_the_same_seed_produce_identical_cohort_hashes() -> None:
    first = _cohort()
    second = _cohort()
    assert first.content_hash() == second.content_hash()


def test_a_different_seed_produces_a_different_hash() -> None:
    first = generate_cohort(seed=SEED, n_customers=50, n_invoices=100, horizon_days=90)
    second = generate_cohort(seed=SEED + 1, n_customers=50, n_invoices=100, horizon_days=90)
    assert first.content_hash() != second.content_hash()


def test_split_is_roughly_50_50_and_stable_across_runs() -> None:
    first = _cohort()
    second = _cohort()
    counts = first.split_counts()
    total = counts["A"] + counts["B"]
    assert total == N_CUSTOMERS
    # "roughly" — not exactly 50/50, but neither half should be a rounding error from the other.
    assert 0.4 <= counts["A"] / total <= 0.6
    assert first.split_counts() == second.split_counts()


def test_exploration_log_is_identical_across_runs_with_the_same_seed() -> None:
    # Thompson sampling (policy/explore.py) draws from an np.random.Generator seeded here,
    # not from a shared/unseeded source -- the log it produces, and the model trained from
    # it, must come out byte-identical across two independent runs.
    small = generate_cohort(seed=SEED, n_customers=40, n_invoices=120, horizon_days=60)
    log_a = generate_exploration_log(small, seed=SEED)
    log_b = generate_exploration_log(small, seed=SEED)
    assert [(e.features, e.success) for e in log_a] == [(e.features, e.success) for e in log_b]

    model_a = HazardModel.train(log_a)
    model_b = HazardModel.train(log_b)
    assert model_a.predict(log_a[0].features) == model_b.predict(log_b[0].features)


def test_split_assignment_is_independent_of_what_else_consumed_the_rng() -> None:
    # Same customer_id, generated under different n_invoices (which changes how much of the
    # rng stream cohort generation consumes downstream) -- split must not move.
    small = generate_cohort(seed=SEED, n_customers=20, n_invoices=10, horizon_days=90)
    large = generate_cohort(seed=SEED, n_customers=20, n_invoices=5000, horizon_days=90)
    small_splits = {c.customer_id: c.split for c in small.customers}
    large_splits = {c.customer_id: c.split for c in large.customers}
    assert small_splits == large_splits
