"""Per-customer posterior over payday day-of-month, inferred from observed attempt outcomes
only — never from the simulator's hidden LatentCustomer.payday_dom (BUILD_DOC.md §3: policy
code only ever sees error codes, not simulator state).

POPULATION_PRIOR is deliberately not read from sim/world.yaml, even though the two are
similar in shape. world.yaml is the simulator's generative ground truth; this prior is meant
to model what a real system would start from with no data of its own — Razorpay's own
published payday guidance. Sourcing the prior from the same file that generates the "true"
answer would make "our posterior rediscovers Razorpay's guidance" a circular, meaningless
claim instead of the validation result BUILD_DOC.md §4.3 wants it to be.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime

from vasool.domain.timezones import ist_date

# SOURCED: razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026/
# "avoid debit dates on the 25th to 31st; align to the 3rd to 7th". Read as the same
# three-component shape used to generate the simulator (independently re-derived here, not
# imported from it): a majority near month-start, a minority near the 7th, a minority on the
# last working day.
POPULATION_PRIOR_COMPONENTS: tuple[tuple[float, int, int], ...] = (
    (0.60, 1, 2),  # weight, center day, spread
    (0.15, 7, 3),
    (0.25, 28, 2),  # "last working day" approximated as late-month
)

# Empirical-Bayes shrinkage strength: with `k` pseudo-observations of the population prior,
# a customer needs roughly this many real observations before their own evidence dominates
# the shrinkage. BUILD_DOC.md's build-challenges note: "fewer than three historical successes
# is unidentifiable" — k=5 keeps low-observation customers solidly prior-dominated.
PRIOR_STRENGTH = 5.0

# Below this posterior peak, the planner doesn't trust the inference enough to act on it.
CONFIDENCE_THRESHOLD = 0.12


def _population_prior_weights() -> list[float]:
    # A small floor on every day, not just the three clustered components -- a hard zero
    # prior would mean no amount of evidence could ever move the posterior onto that day
    # (0 * any likelihood is still 0), which is bad Bayesian practice, not a real belief.
    floor = 0.001
    weights = [0.0] + [floor] * 31  # index 0 unused, days 1..31
    for share, center, spread in POPULATION_PRIOR_COMPONENTS:
        days = range(max(1, center - spread), min(31, center + spread) + 1)
        per_day = share / len(days)
        for day in days:
            weights[day] += per_day
    total = sum(weights)
    return [w / total for w in weights]


POPULATION_PRIOR_WEIGHTS = _population_prior_weights()


@dataclass(frozen=True)
class PaydayObservation:
    day_of_month: int
    insufficient_funds: bool  # True: balance was demonstrably low that day (negative evidence)


def build_evidence(
    customer_invoices: list[tuple[datetime, bool]],
) -> tuple[PaydayObservation, ...]:
    """`customer_invoices` is (first_failed_at, was_insufficient_funds) for a customer's
    other invoices — the harness builds this from Cohort.origin_failures, never from
    LatentCustomer directly.
    """
    return tuple(
        PaydayObservation(day_of_month=ist_date(occurred_at).day, insufficient_funds=was_if)
        for occurred_at, was_if in customer_invoices
    )


@dataclass(frozen=True)
class PaydayPosterior:
    weights: tuple[float, ...]  # index 0 unused, days 1..31, sums to 1.0
    n_observations: int

    @classmethod
    def infer(cls, evidence: tuple[PaydayObservation, ...]) -> PaydayPosterior:
        observed = list(POPULATION_PRIOR_WEIGHTS)
        for obs in evidence:
            # A day observed as insufficient-funds is evidence against payday being that
            # day; a day observed failing for any other reason is weak evidence for it
            # (balance wasn't obviously the problem there).
            multiplier = 0.3 if obs.insufficient_funds else 1.2
            observed[obs.day_of_month] *= multiplier
        total = sum(observed)
        observed = [w / total for w in observed]

        n = len(evidence)
        shrink = n / (n + PRIOR_STRENGTH)
        shrunk = [
            shrink * observed[day] + (1 - shrink) * POPULATION_PRIOR_WEIGHTS[day] for day in range(32)
        ]
        total = sum(shrunk)
        shrunk = [w / total for w in shrunk]
        return cls(weights=tuple(shrunk), n_observations=n)

    def map_estimate(self) -> int:
        return max(range(1, 32), key=lambda day: self.weights[day])

    def confidence(self) -> float:
        # Posterior concentration alone isn't enough -- the *prior* is already peaky (it's
        # built from three clustered components), so a customer with zero observations
        # would otherwise look just as "confident" as one with twenty. Scaling by how much
        # evidence actually contributed (the same shrinkage weight used to build the
        # posterior) means confidence is genuinely zero with no evidence and grows toward
        # the raw peak as observations accumulate.
        evidence_weight = self.n_observations / (self.n_observations + PRIOR_STRENGTH)
        return evidence_weight * max(self.weights[1:])

    def credible_interval(self, mass: float = 0.8) -> tuple[int, int]:
        """Smallest contiguous day-of-month range (wrapping is not modeled) covering `mass`
        of the posterior, expanding outward from the MAP estimate.
        """
        center = self.map_estimate()
        low, high = center, center
        covered = self.weights[center]
        while covered < mass and (low > 1 or high < 31):
            expand_low = self.weights[low - 1] if low > 1 else -1.0
            expand_high = self.weights[high + 1] if high < 31 else -1.0
            if expand_low >= expand_high:
                low -= 1
                covered += self.weights[low]
            else:
                high += 1
                covered += self.weights[high]
        return low, high

    def is_confident(self, threshold: float = CONFIDENCE_THRESHOLD) -> bool:
        return self.confidence() >= threshold


def next_occurrence(day_of_month: int, after: date) -> date:
    """The next calendar date carrying `day_of_month`, strictly after `after`, clamped to
    the real length of whatever month it lands in.
    """
    year, month = after.year, after.month
    candidate = _clamp(year, month, day_of_month)
    if candidate <= after:
        month += 1
        if month > 12:
            month = 1
            year += 1
        candidate = _clamp(year, month, day_of_month)
    return candidate


def _clamp(year: int, month: int, day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))
