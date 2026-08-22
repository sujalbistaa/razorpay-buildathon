"""Thompson sampling — Beta posterior per (failure_class, time_bucket) cell, applied only
where evidence is thin. Agrawal & Goyal, "Thompson Sampling for Contextual Bandits with
Linear Payoffs", ICML 2013.

CLAUDE.md invariant 2: posteriors update on observed outcomes only. No LLM output ever
enters a posterior, an arm choice, or a reward here -- this module is the system using
probability, not the LLM computing it.

Two call sites, both explained here because neither is obvious from the Policy Protocol
alone (Policy.plan() has no outcome-callback hook, so there's no way for a live posterior
update to happen mid-plan):

1. bench/exploration.py, at *training-log generation* time: builds the varied
   (attempt_context, slot) -> success log hazard.py trains on, by sometimes trying a
   Thompson-sampled slot instead of always the greedy-best one, and updating the posterior
   after each simulated outcome. This is the actual "learns without spending real money on
   bad arms" loop BUILD_DOC.md §4.3 describes.
2. policy/planner.py, at *serving* time: the posterior fit during step 1 is frozen and
   consulted only as an uncertainty gate -- cells with little evidence fall back to a
   Thompson draw from that frozen posterior instead of trusting a hazard-model point
   estimate it was barely trained on. It is not updated again inside a single plan() call.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import numpy as np

from vasool.domain.types import FailureClass

# Uninformative-ish Beta(1,1) prior pseudocounts. A cell needs at least this many real
# pseudo-observations above the prior before the hazard model's own estimate is trusted over
# a Thompson draw -- see is_uncertain().
PRIOR_ALPHA = 1.0
PRIOR_BETA = 1.0
UNCERTAINTY_THRESHOLD_OBSERVATIONS = 20.0


class ExploreCell(NamedTuple):
    failure_class: FailureClass
    time_bucket: int  # caller-defined bucketing (e.g. days-since-failure // N)


@dataclass(frozen=True)
class BetaPosterior:
    alpha: float = PRIOR_ALPHA
    beta: float = PRIOR_BETA

    def update(self, success: bool) -> BetaPosterior:
        return BetaPosterior(self.alpha + 1, self.beta) if success else BetaPosterior(self.alpha, self.beta + 1)

    @property
    def n_observations(self) -> float:
        return (self.alpha - PRIOR_ALPHA) + (self.beta - PRIOR_BETA)

    def mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


@dataclass(frozen=True)
class ThompsonExplorer:
    posteriors: dict[ExploreCell, BetaPosterior]

    @classmethod
    def empty(cls) -> ThompsonExplorer:
        return cls(posteriors={})

    def _posterior(self, cell: ExploreCell) -> BetaPosterior:
        return self.posteriors.get(cell, BetaPosterior())

    def is_uncertain(self, cell: ExploreCell, threshold: float = UNCERTAINTY_THRESHOLD_OBSERVATIONS) -> bool:
        return self._posterior(cell).n_observations < threshold

    def sample(self, cell: ExploreCell, rng: np.random.Generator) -> float:
        posterior = self._posterior(cell)
        return float(rng.beta(posterior.alpha, posterior.beta))

    def decide(self, cell: ExploreCell, hazard_probability: float, rng: np.random.Generator) -> float:
        """The probability planner.py should actually use for `cell`: the hazard model's own
        estimate where there's enough evidence to trust it, otherwise a Thompson draw from
        this (possibly still-thin) posterior -- exploring rather than pretending confidence
        the data doesn't support.
        """
        if self.is_uncertain(cell):
            return self.sample(cell, rng)
        return hazard_probability

    def update(self, cell: ExploreCell, success: bool) -> ThompsonExplorer:
        updated = dict(self.posteriors)
        updated[cell] = self._posterior(cell).update(success)
        return ThompsonExplorer(posteriors=updated)
