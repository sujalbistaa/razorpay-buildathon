from datetime import date

from vasool.policy.payday import PaydayObservation, PaydayPosterior, next_occurrence


def test_no_evidence_matches_population_prior_shape() -> None:
    posterior = PaydayPosterior.infer(())
    # BUILD_DOC.md §1.3: avoid 25th-31st, align 3rd-7th -- with no evidence the MAP estimate
    # should land in one of the sourced clusters, not an arbitrary mid-month day.
    assert posterior.map_estimate() in range(1, 11)
    assert not posterior.is_confident() or posterior.confidence() > 0


def test_heavy_negative_evidence_moves_the_estimate_off_the_prior_peak() -> None:
    baseline = PaydayPosterior.infer(())
    peak_day = baseline.map_estimate()
    evidence = tuple(
        PaydayObservation(day_of_month=peak_day, insufficient_funds=True) for _ in range(20)
    )
    posterior = PaydayPosterior.infer(evidence)
    assert posterior.map_estimate() != peak_day


def test_confidence_increases_with_more_consistent_evidence() -> None:
    few = PaydayPosterior.infer((PaydayObservation(day_of_month=5, insufficient_funds=False),) * 2)
    many = PaydayPosterior.infer((PaydayObservation(day_of_month=5, insufficient_funds=False),) * 20)
    assert many.confidence() > few.confidence()


def test_credible_interval_contains_the_map_estimate() -> None:
    posterior = PaydayPosterior.infer((PaydayObservation(day_of_month=5, insufficient_funds=False),) * 10)
    low, high = posterior.credible_interval()
    assert low <= posterior.map_estimate() <= high


def test_next_occurrence_within_same_month() -> None:
    assert next_occurrence(15, date(2026, 1, 1)) == date(2026, 1, 15)


def test_next_occurrence_rolls_to_next_month_when_already_passed() -> None:
    assert next_occurrence(15, date(2026, 1, 20)) == date(2026, 2, 15)


def test_next_occurrence_clamps_to_end_of_short_month() -> None:
    assert next_occurrence(31, date(2026, 1, 15)) == date(2026, 1, 31)
    assert next_occurrence(31, date(2026, 2, 1)) == date(2026, 2, 28)


def test_next_occurrence_is_strictly_after_the_given_date() -> None:
    assert next_occurrence(15, date(2026, 1, 15)) == date(2026, 2, 15)
