"""HazardModel: trains on a synthetic signal it should actually recover, and never raises
on a missing or corrupt model file -- CLAUDE.md: "Model file missing -> HeuristicPolicy,"
a fallback trigger, never an exception that escapes.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from vasool.domain.money import Money
from vasool.domain.types import FailureClass, Rail
from vasool.policy.hazard import (
    MIN_TRAINING_EXAMPLES,
    HazardExample,
    HazardFeatures,
    HazardModel,
    amount_bucket,
    hour_bucket,
)


def _features(**overrides: object) -> HazardFeatures:
    defaults: dict[str, object] = {
        "failure_class": FailureClass.INSUFFICIENT_FUNDS,
        "days_since_failure": 3.0,
        "days_relative_to_payday": 0.0,
        "issuer_up": True,
        "attempt_index": 0,
        "amount": Money.from_rupees(500),
        "rail": Rail.UPI_AUTOPAY,
        "hour": 10,
    }
    defaults.update(overrides)
    return HazardFeatures(**defaults)  # type: ignore[arg-type]


def _synthetic_examples(n: int, seed: int = 1) -> list[HazardExample]:
    rng = random.Random(seed)
    examples = []
    for _ in range(n):
        issuer_up = rng.random() > 0.3
        p = 0.85 if issuer_up else 0.05
        examples.append(HazardExample(_features(issuer_up=issuer_up, days_since_failure=rng.uniform(0, 14)), rng.random() < p))
    return examples


@pytest.mark.parametrize(
    ("paise", "expected"),
    [
        (1, "b0"),
        (30_000, "b0"),
        (30_001, "b1"),
        (70_000, "b1"),
        (150_000, "b2"),
        (500_000, "b3"),
        (500_001, "b4"),
    ],
)
def test_amount_bucket_boundaries(paise: int, expected: str) -> None:
    assert amount_bucket(Money(paise)) == expected


@pytest.mark.parametrize(("hour", "expected"), [(0, 0), (5, 0), (6, 1), (11, 1), (12, 2), (17, 2), (18, 3), (23, 3)])
def test_hour_bucket_boundaries(hour: int, expected: int) -> None:
    assert hour_bucket(hour) == expected


def test_train_raises_below_minimum_examples() -> None:
    with pytest.raises(ValueError, match="need >="):
        HazardModel.train(_synthetic_examples(MIN_TRAINING_EXAMPLES - 1))


def test_train_raises_on_single_outcome_class() -> None:
    examples = [HazardExample(_features(), True) for _ in range(MIN_TRAINING_EXAMPLES + 10)]
    with pytest.raises(ValueError, match="only one outcome class"):
        HazardModel.train(examples)


def test_model_recovers_the_dominant_synthetic_signal() -> None:
    model = HazardModel.train(_synthetic_examples(500))
    p_up = model.predict(_features(issuer_up=True))
    p_down = model.predict(_features(issuer_up=False))
    assert p_up > p_down
    assert p_up > 0.5
    assert p_down < 0.5


def test_save_and_load_round_trips_predictions(tmp_path: Path) -> None:
    model = HazardModel.train(_synthetic_examples(500))
    path = tmp_path / "hazard.txt"
    model.save(path)
    loaded = HazardModel.load(path)
    assert loaded is not None
    assert loaded.predict(_features(issuer_up=True)) == pytest.approx(model.predict(_features(issuer_up=True)))


def test_load_returns_none_for_missing_file(tmp_path: Path) -> None:
    assert HazardModel.load(tmp_path / "does_not_exist.txt") is None


def test_load_returns_none_for_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.txt"
    path.write_text("this is not a lightgbm model")
    assert HazardModel.load(path) is None
