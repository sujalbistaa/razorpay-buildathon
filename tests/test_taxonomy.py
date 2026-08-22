from vasool.domain.taxonomy import (
    DEFAULT_SOURCE,
    HARD_DECLINE_CLASSES,
    RECOVERABILITY,
    Recoverability,
)
from vasool.domain.types import FailureClass


def test_every_failure_class_is_classified_for_recoverability() -> None:
    assert set(RECOVERABILITY.keys()) == set(FailureClass)


def test_every_failure_class_has_a_default_source() -> None:
    assert set(DEFAULT_SOURCE.keys()) == set(FailureClass)


def test_hard_decline_classes_matches_hard_recoverability_exactly() -> None:
    hard_from_table = {
        fc for fc, recoverability in RECOVERABILITY.items() if recoverability is Recoverability.HARD
    }
    assert HARD_DECLINE_CLASSES == hard_from_table
    assert HARD_DECLINE_CLASSES == {
        FailureClass.CARD_EXPIRED,
        FailureClass.DEBIT_INSTRUMENT_BLOCKED,
        FailureClass.MANDATE_REVOKED,
        FailureClass.PAYMENT_RISK_CHECK_FAILED,
    }
