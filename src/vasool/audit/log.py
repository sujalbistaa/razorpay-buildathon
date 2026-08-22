"""Append-only decision log — invariant 7. record_decision() writes before the action
executes; record_outcome() writes to a separate table afterwards. Neither ever updates or
deletes a row: there is no update_decision() on this class, on purpose.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import Field, Session, SQLModel, create_engine

from vasool.domain.types import Decision


class DecisionRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    invoice_id: str = Field(index=True)
    attempt_index: int
    decided_at: datetime
    input_snapshot_hash: str
    policy_version: str
    compliance_approved: bool
    compliance_results_json: str
    chosen_action: str
    expected_value_paise: int


class OutcomeRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    invoice_id: str = Field(index=True)
    attempt_index: int
    recorded_at: datetime
    success: bool
    recovered_paise: int
    failure_reason: str | None


class AuditLog:
    def __init__(self, db_path: str) -> None:
        self._engine = create_engine(f"sqlite:///{db_path}")
        SQLModel.metadata.create_all(self._engine)

    def record_decision(self, decision: Decision) -> None:
        row = DecisionRow(
            invoice_id=decision.invoice_id,
            attempt_index=decision.attempt_index,
            decided_at=decision.decided_at,
            input_snapshot_hash=decision.input_snapshot_hash,
            policy_version=decision.policy_version,
            compliance_approved=decision.compliance_verdict.approved,
            compliance_results_json=json.dumps(
                [{"rule_id": r.rule_id, "passed": r.passed, "reason": r.reason} for r in decision.compliance_verdict.results]
            ),
            chosen_action=decision.chosen_action.value,
            expected_value_paise=decision.expected_value.paise,
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()

    def record_outcome(
        self,
        invoice_id: str,
        attempt_index: int,
        recorded_at: datetime,
        success: bool,
        recovered_paise: int,
        failure_reason: str | None,
    ) -> None:
        row = OutcomeRow(
            invoice_id=invoice_id,
            attempt_index=attempt_index,
            recorded_at=recorded_at,
            success=success,
            recovered_paise=recovered_paise,
            failure_reason=failure_reason,
        )
        with Session(self._engine) as session:
            session.add(row)
            session.commit()
