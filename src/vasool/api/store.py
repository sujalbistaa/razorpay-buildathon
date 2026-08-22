"""Live-mode persistence — distinct from bench/'s in-memory Cohort (used for benchmark runs)
and audit/log.py's append-only decision log (used by both bench and live). This is the state
a webhook-driven system needs on top of those: which invoices are currently open, which
webhook deliveries have already been processed (dedup), any known issuer downtime, and any
processing failure that needs a human to look at and replay (BUILD_DOC.md §7: "poison queue
message -> dead-letter table + /admin/dlq/replay endpoint").
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import Field, Session, SQLModel, create_engine, select


class WebhookEventRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    event_id: str = Field(unique=True, index=True)  # x-razorpay-event-id
    event_type: str
    received_at: datetime
    payload_json: str


class LiveInvoiceRow(SQLModel, table=True):
    invoice_id: str = Field(primary_key=True)
    customer_id: str
    amount_paise: int
    status: str  # "open" | "recovered" | "stopped"
    failure_class: str | None = None
    classification_confidence: float | None = None
    stop_rule: str | None = None
    updated_at: datetime


class DowntimeEventRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    issuer: str
    method: str
    severity: str
    begin: datetime
    end: datetime | None
    scheduled: bool = False
    resolved: bool = False


class DeadLetterRow(SQLModel, table=True):
    id: int | None = Field(default=None, primary_key=True)
    event_id: str
    event_type: str
    payload_json: str
    error: str
    failed_at: datetime
    replayed: bool = False


class LiveStore:
    def __init__(self, db_path: str) -> None:
        self._engine = create_engine(f"sqlite:///{db_path}")
        SQLModel.metadata.create_all(self._engine)

    def try_record_event(self, event_id: str, event_type: str, payload: dict[str, Any], now: datetime) -> bool:
        """True the first time `event_id` is seen (and records it); False if already
        processed. Dedup is enforced by the UNIQUE constraint on `event_id`, not a
        read-then-write check -- this is race-safe against two concurrent deliveries of the
        same webhook, which a SELECT-then-INSERT would not be.
        """
        row = WebhookEventRow(event_id=event_id, event_type=event_type, received_at=now, payload_json=json.dumps(payload))
        try:
            with Session(self._engine) as session:
                session.add(row)
                session.commit()
            return True
        except IntegrityError:
            return False

    def upsert_invoice(
        self, invoice_id: str, customer_id: str, amount_paise: int, status: str, now: datetime, *,
        failure_class: str | None = None, classification_confidence: float | None = None, stop_rule: str | None = None,
    ) -> None:
        with Session(self._engine) as session:
            existing = session.get(LiveInvoiceRow, invoice_id)
            if existing is None:
                session.add(LiveInvoiceRow(
                    invoice_id=invoice_id, customer_id=customer_id, amount_paise=amount_paise, status=status,
                    failure_class=failure_class, classification_confidence=classification_confidence,
                    stop_rule=stop_rule, updated_at=now,
                ))
            else:
                existing.status = status
                existing.amount_paise = amount_paise
                if failure_class is not None:
                    existing.failure_class = failure_class
                if classification_confidence is not None:
                    existing.classification_confidence = classification_confidence
                if stop_rule is not None:
                    existing.stop_rule = stop_rule
                existing.updated_at = now
                session.add(existing)
            session.commit()

    def get_invoice(self, invoice_id: str) -> LiveInvoiceRow | None:
        with Session(self._engine) as session:
            return session.get(LiveInvoiceRow, invoice_id)

    def list_open_invoices(self) -> list[LiveInvoiceRow]:
        with Session(self._engine) as session:
            return list(session.exec(select(LiveInvoiceRow).where(LiveInvoiceRow.status == "open")))

    def record_downtime(self, issuer: str, method: str, severity: str, begin: datetime, end: datetime | None, scheduled: bool) -> None:
        with Session(self._engine) as session:
            session.add(DowntimeEventRow(issuer=issuer, method=method, severity=severity, begin=begin, end=end, scheduled=scheduled))
            session.commit()

    def resolve_downtime(self, issuer: str, method: str, resolved_at: datetime) -> None:
        with Session(self._engine) as session:
            open_windows = session.exec(
                select(DowntimeEventRow).where(
                    DowntimeEventRow.issuer == issuer, DowntimeEventRow.method == method, DowntimeEventRow.resolved == False
                )
            )
            for window in open_windows:
                window.resolved = True
                window.end = resolved_at
                session.add(window)
            session.commit()

    def list_downtime(self) -> list[DowntimeEventRow]:
        with Session(self._engine) as session:
            return list(session.exec(select(DowntimeEventRow)))

    def record_dead_letter(self, event_id: str, event_type: str, payload: dict[str, Any], error: str, now: datetime) -> None:
        with Session(self._engine) as session:
            session.add(DeadLetterRow(
                event_id=event_id, event_type=event_type, payload_json=json.dumps(payload), error=error,
                failed_at=now, replayed=False,
            ))
            session.commit()

    def list_dead_letters(self, *, include_replayed: bool = False) -> list[DeadLetterRow]:
        with Session(self._engine) as session:
            statement = select(DeadLetterRow)
            if not include_replayed:
                statement = statement.where(DeadLetterRow.replayed == False)
            return list(session.exec(statement))

    def get_dead_letter(self, dlq_id: int) -> DeadLetterRow | None:
        with Session(self._engine) as session:
            return session.get(DeadLetterRow, dlq_id)

    def mark_replayed(self, dlq_id: int) -> None:
        with Session(self._engine) as session:
            row = session.get(DeadLetterRow, dlq_id)
            if row is not None:
                row.replayed = True
                session.add(row)
                session.commit()
