"""Discrete-time hazard model — BUILD_DOC.md §4.3: P(success | failure_class,
days_since_failure, days_relative_to_inferred_payday, issuer_up(t), attempt_index,
amount_bucket, rail, hour_bucket). Gradient-boosted logistic regression (LightGBM), not a
neural network: a few thousand exploration attempts, tabular features, needs to be
explainable to a compliance reviewer.

Trained on an exploration log generated from cohort A only (bench/exploration.py);
LearnedPolicy (learned.py) evaluates exclusively on held-out cohort B. This module has no
opinion on which cohort a HazardExample came from -- that split is enforced by the caller.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import lightgbm as lgb
import pandas as pd

from vasool.domain.money import Money
from vasool.domain.types import FailureClass, Rail
from vasool.logging import get_logger

logger = get_logger(__name__)

# Fixed, small feature order -- every HazardFeatures encodes to exactly these columns, in
# this order, so a saved Booster and a freshly-encoded row always line up.
FEATURE_COLUMNS: tuple[str, ...] = (
    "failure_class",
    "days_since_failure",
    "days_relative_to_payday",
    "issuer_up",
    "attempt_index",
    "amount_bucket",
    "rail",
    "hour_bucket",
)
CATEGORICAL_COLUMNS: tuple[str, ...] = ("failure_class", "amount_bucket", "rail")

# Modeling choice, not a compliance constant -- no source needed, just documented reasoning.
# Log-spaced bands covering world.yaml's invoice amount distribution (median ~INR 665,
# lognormal mean_log=6.5 sigma_log=0.6), coarse enough that each bucket sees enough training
# rows from a ~2,000-invoice cohort.
AMOUNT_BUCKET_EDGES_PAISE: tuple[int, ...] = (30_000, 70_000, 150_000, 500_000)  # INR 300/700/1500/5000


def amount_bucket(amount: Money) -> str:
    for i, edge in enumerate(AMOUNT_BUCKET_EDGES_PAISE):
        if amount.paise <= edge:
            return f"b{i}"
    return f"b{len(AMOUNT_BUCKET_EDGES_PAISE)}"


def hour_bucket(hour: int) -> int:
    # Four 6-hour bands -- coarse enough to be learnable from a few thousand rows, without
    # asserting any particular business meaning about which band matters (that's what the
    # model is for).
    return hour // 6


@dataclass(frozen=True)
class HazardFeatures:
    failure_class: FailureClass
    days_since_failure: float
    days_relative_to_payday: float  # candidate day-of-month minus inferred payday day-of-month
    issuer_up: bool
    attempt_index: int
    amount: Money
    rail: Rail
    hour: int

    def to_row(self) -> dict[str, Any]:
        return {
            "failure_class": self.failure_class.value,
            "days_since_failure": float(self.days_since_failure),
            "days_relative_to_payday": float(self.days_relative_to_payday),
            "issuer_up": bool(self.issuer_up),
            "attempt_index": int(self.attempt_index),
            "amount_bucket": amount_bucket(self.amount),
            "rail": self.rail.value,
            "hour_bucket": hour_bucket(self.hour),
        }


@dataclass(frozen=True)
class HazardExample:
    features: HazardFeatures
    success: bool


def _to_frame(examples: Sequence[HazardFeatures]) -> pd.DataFrame:
    frame = pd.DataFrame([f.to_row() for f in examples], columns=list(FEATURE_COLUMNS))
    for col in CATEGORICAL_COLUMNS:
        frame[col] = frame[col].astype("category")
    return frame


# Deliberately shallow -- BUILD_DOC.md §4.3: "a few thousand attempts, tabular features,
# needs to be explainable to a compliance reviewer." A large boosted ensemble would overfit
# a training log this size and defeats the explainability argument for choosing LightGBM
# over a neural network in the first place.
NUM_LEAVES = 15
LEARNING_RATE = 0.05
NUM_BOOST_ROUND = 100
MIN_DATA_IN_LEAF = 20

# Below this many training rows, or with only one observed outcome class, a binary
# classifier can't be fit meaningfully -- train() raises rather than silently returning a
# model that always predicts one constant.
MIN_TRAINING_EXAMPLES = 50


@dataclass(frozen=True)
class HazardModel:
    booster: lgb.Booster

    @classmethod
    def train(cls, examples: Sequence[HazardExample]) -> HazardModel:
        if len(examples) < MIN_TRAINING_EXAMPLES:
            raise ValueError(f"need >= {MIN_TRAINING_EXAMPLES} examples to train, got {len(examples)}")
        labels = [1 if e.success else 0 for e in examples]
        if len(set(labels)) < 2:
            raise ValueError("training examples contain only one outcome class")

        frame = _to_frame([e.features for e in examples])
        dataset = lgb.Dataset(frame, label=labels, categorical_feature=list(CATEGORICAL_COLUMNS), free_raw_data=False)
        params = {
            "objective": "binary",
            "num_leaves": NUM_LEAVES,
            "learning_rate": LEARNING_RATE,
            "min_data_in_leaf": MIN_DATA_IN_LEAF,
            "verbose": -1,
        }
        booster = lgb.train(params, dataset, num_boost_round=NUM_BOOST_ROUND)
        return cls(booster=booster)

    def predict(self, features: HazardFeatures) -> float:
        return self.predict_many([features])[0]

    def predict_many(self, features: Sequence[HazardFeatures]) -> list[float]:
        # planner.py scores a whole day's worth of candidate slots per planning step; one
        # batched booster.predict() call over all of them is far cheaper than calling
        # predict() (a fresh single-row DataFrame + a full booster pass) once per candidate.
        frame = _to_frame(features)
        predictions = self.booster.predict(frame)
        return [float(p) for p in predictions]

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.booster.save_model(str(path))

    @classmethod
    def load(cls, path: Path) -> HazardModel | None:
        # Missing or corrupt model artefact is a fallback trigger, never an exception that
        # escapes -- CLAUDE.md: "Model file missing -> HeuristicPolicy." Callers (learned.py)
        # treat None as "go degraded," never crash. Logged here (not swallowed silently) even
        # though the caller also logs its own fallback -- this is the one place that knows
        # *why* the model didn't load.
        if not path.exists():
            logger.warning("hazard_model_missing", path=str(path))
            return None
        try:
            booster = lgb.Booster(model_file=str(path))
        except Exception as exc:  # noqa: BLE001 -- a corrupt model file is a fallback trigger, not a crash; LightGBM raises several unrelated exception types for a malformed model file.
            logger.warning("hazard_model_corrupt", path=str(path), error=str(exc))
            return None
        return cls(booster=booster)
