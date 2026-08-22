"""SimulatorClient idempotency (invariant 6) and AuditLog's append-only behavior (invariant 7)."""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlmodel import Session, select

from vasool.audit.log import AuditLog, DecisionRow, OutcomeRow
from vasool.domain.money import Money
from vasool.domain.types import (
    ActionType,
    Attempt,
    ComplianceVerdict,
    Decision,
    RuleResult,
)
from vasool.execute.simulator_client import SimulatorClient
from vasool.sim.cohort import generate_cohort

NOW = datetime(2026, 6, 1, tzinfo=UTC)


def _decision(invoice_id: str, attempt_index: int) -> Decision:
    return Decision(
        invoice_id=invoice_id,
        attempt_index=attempt_index,
        decided_at=NOW,
        input_snapshot_hash="deadbeef",
        policy_version="test:v1",
        compliance_verdict=ComplianceVerdict(approved=True, results=(RuleResult(rule_id="R001_PRE_DEBIT_NOTICE", passed=True),)),
        chosen_action=ActionType.SILENT_RETRY,
        expected_value=Money.from_rupees(100),
    )


def test_simulator_client_caches_by_idempotency_key() -> None:
    cohort = generate_cohort(seed=1, n_customers=5, n_invoices=10, horizon_days=30)
    invoice = cohort.invoices[0]
    customer = cohort.world.customer(invoice.customer_id)
    attempt = Attempt(
        invoice_id=invoice.invoice_id,
        attempt_index=0,
        action_type=ActionType.SILENT_RETRY,
        rail=customer.mandate_rail,
        amount=invoice.amount,
        notify_at=None,
        debit_at=invoice.first_failed_at,
    )
    client = SimulatorClient(cohort.world)
    first = client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)
    second = client.execute(invoice, attempt, invoice.first_failed_at, attempt.idempotency_key)
    assert first == second
    # Cached, not re-simulated: same idempotency_key must not touch World.attempt() twice.
    assert client._seen[attempt.idempotency_key] is first


def test_simulator_client_does_not_cache_across_different_keys() -> None:
    cohort = generate_cohort(seed=1, n_customers=5, n_invoices=10, horizon_days=30)
    invoice = cohort.invoices[0]
    customer = cohort.world.customer(invoice.customer_id)
    client = SimulatorClient(cohort.world)
    attempt_a = Attempt(
        invoice_id=invoice.invoice_id, attempt_index=0, action_type=ActionType.SILENT_RETRY,
        rail=customer.mandate_rail, amount=invoice.amount, notify_at=None, debit_at=invoice.first_failed_at,
    )
    attempt_b = Attempt(
        invoice_id=invoice.invoice_id, attempt_index=1, action_type=ActionType.SILENT_RETRY,
        rail=customer.mandate_rail, amount=invoice.amount, notify_at=None, debit_at=invoice.first_failed_at,
    )
    client.execute(invoice, attempt_a, invoice.first_failed_at, attempt_a.idempotency_key)
    client.execute(invoice, attempt_b, invoice.first_failed_at, attempt_b.idempotency_key)
    assert len(client._seen) == 2


def test_audit_log_record_decision_and_outcome_are_append_only() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        db_path = str(Path(tmp) / "audit.db")
        audit = AuditLog(db_path)
        audit.record_decision(_decision("inv_1", 0))
        audit.record_decision(_decision("inv_1", 1))
        audit.record_outcome("inv_1", 0, NOW, True, 10_000, None)

        with Session(audit._engine) as session:
            decisions = session.exec(select(DecisionRow)).all()
            outcomes = session.exec(select(OutcomeRow)).all()

        assert len(decisions) == 2
        assert len(outcomes) == 1
        assert decisions[0].invoice_id == "inv_1"
        assert decisions[0].attempt_index == 0
        assert decisions[1].attempt_index == 1


def test_audit_log_has_no_update_method() -> None:
    # Structural guarantee, not just convention: nothing on this class can mutate a row.
    public_methods = {name for name in dir(AuditLog) if not name.startswith("_")}
    assert public_methods == {"record_decision", "record_outcome"}
