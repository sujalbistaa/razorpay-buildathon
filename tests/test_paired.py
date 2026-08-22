"""paired_bootstrap_ci: a pure numeric routine, tested without any Cohort machinery."""

from __future__ import annotations

import pytest

from vasool.bench.paired import paired_bootstrap_ci


def test_raises_on_mismatched_lengths() -> None:
    with pytest.raises(ValueError, match="same length"):
        paired_bootstrap_ci([1, 2], [1], seed=1)


def test_raises_on_empty_input() -> None:
    with pytest.raises(ValueError, match="no paired"):
        paired_bootstrap_ci([], [], seed=1)


def test_identical_arms_have_zero_mean_diff_and_a_tight_ci() -> None:
    values = [100, 200, 0, 500, 300] * 20
    result = paired_bootstrap_ci(values, values, seed=1)
    assert result.mean_diff_paise == 0.0
    assert result.low_paise == pytest.approx(0.0, abs=1e-9)
    assert result.high_paise == pytest.approx(0.0, abs=1e-9)


def test_a_consistent_uplift_produces_a_positive_ci_excluding_zero() -> None:
    baseline = [100] * 50
    treatment = [200] * 50
    result = paired_bootstrap_ci(baseline, treatment, seed=1)
    assert result.mean_diff_paise == 100.0
    assert result.low_paise > 0


def test_result_is_deterministic_for_the_same_seed() -> None:
    baseline = [10, 50, 0, 200, 30, 40, 0, 90]
    treatment = [20, 40, 10, 210, 25, 60, 5, 95]
    a = paired_bootstrap_ci(baseline, treatment, seed=7)
    b = paired_bootstrap_ci(baseline, treatment, seed=7)
    assert a == b


def test_different_seeds_can_move_the_ci_bounds_slightly() -> None:
    baseline = [10, 50, 0, 200, 30, 40, 0, 90]
    treatment = [20, 40, 10, 210, 25, 60, 5, 95]
    a = paired_bootstrap_ci(baseline, treatment, seed=1)
    b = paired_bootstrap_ci(baseline, treatment, seed=2)
    assert a.mean_diff_paise == b.mean_diff_paise  # the point estimate doesn't depend on resampling
