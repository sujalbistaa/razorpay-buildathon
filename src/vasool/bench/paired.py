"""Paired bootstrap confidence interval on a per-invoice metric difference between two arms
run against the identical cohort — BUILD_DOC.md §8: "Report the paired difference with a
bootstrap 95% CI." Seeded (invariant 8): the resampling itself must be reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class BootstrapCI:
    mean_diff_paise: float
    low_paise: float
    high_paise: float
    n_pairs: int
    n_resamples: int


def paired_bootstrap_ci(
    baseline_paise: list[int], treatment_paise: list[int], seed: int, n_resamples: int = 2000, confidence: float = 0.95
) -> BootstrapCI:
    """`baseline_paise` and `treatment_paise` must already be aligned by invoice (same order,
    same invoice on each side) -- this function has no invoice identity of its own, on
    purpose, so it stays a pure numeric routine callers can unit-test without a Cohort.
    """
    if len(baseline_paise) != len(treatment_paise):
        raise ValueError("paired arrays must be the same length")
    if not baseline_paise:
        raise ValueError("no paired observations")

    diffs = np.array(treatment_paise, dtype=float) - np.array(baseline_paise, dtype=float)
    rng = np.random.default_rng(seed)
    n = len(diffs)
    resample_means = np.array([rng.choice(diffs, size=n, replace=True).mean() for _ in range(n_resamples)])
    alpha = (1 - confidence) / 2
    low, high = np.quantile(resample_means, [alpha, 1 - alpha])
    return BootstrapCI(
        mean_diff_paise=float(diffs.mean()), low_paise=float(low), high_paise=float(high),
        n_pairs=n, n_resamples=n_resamples,
    )
