"""ThompsonExplorer: pure posterior update/query, no shared mutable RNG state -- every draw
takes an explicit np.random.Generator (CLAUDE.md invariant 8: no unseeded random)."""

from __future__ import annotations

import numpy as np

from vasool.domain.types import FailureClass
from vasool.policy.explore import BetaPosterior, ExploreCell, ThompsonExplorer

CELL = ExploreCell(FailureClass.INSUFFICIENT_FUNDS, 0)


def test_empty_explorer_is_uncertain_everywhere() -> None:
    explorer = ThompsonExplorer.empty()
    assert explorer.is_uncertain(CELL)


def test_update_reduces_uncertainty_as_evidence_accumulates() -> None:
    explorer = ThompsonExplorer.empty()
    for _ in range(30):
        explorer = explorer.update(CELL, success=True)
    assert not explorer.is_uncertain(CELL)


def test_update_does_not_mutate_the_original() -> None:
    explorer = ThompsonExplorer.empty()
    updated = explorer.update(CELL, success=True)
    assert explorer.posteriors == {}
    assert updated.posteriors[CELL].n_observations == 1


def test_update_is_isolated_per_cell() -> None:
    other = ExploreCell(FailureClass.CARD_DECLINED, 0)
    explorer = ThompsonExplorer.empty().update(CELL, success=True)
    assert explorer._posterior(other).n_observations == 0


def test_mean_moves_toward_observed_outcomes() -> None:
    posterior = BetaPosterior()
    for _ in range(50):
        posterior = posterior.update(success=True)
    assert posterior.mean() > 0.9


def test_decide_uses_hazard_estimate_once_confident() -> None:
    explorer = ThompsonExplorer.empty()
    for _ in range(50):
        explorer = explorer.update(CELL, success=True)
    rng = np.random.default_rng(1)
    assert explorer.decide(CELL, hazard_probability=0.42, rng=rng) == 0.42


def test_decide_samples_from_posterior_when_uncertain() -> None:
    explorer = ThompsonExplorer.empty()
    rng = np.random.default_rng(1)
    value = explorer.decide(CELL, hazard_probability=0.42, rng=rng)
    assert value != 0.42
    assert 0.0 <= value <= 1.0


def test_sample_is_deterministic_for_a_seeded_generator() -> None:
    explorer = ThompsonExplorer.empty().update(CELL, success=True).update(CELL, success=False)
    a = explorer.sample(CELL, np.random.default_rng(99))
    b = explorer.sample(CELL, np.random.default_rng(99))
    assert a == b
