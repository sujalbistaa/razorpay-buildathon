"""The seven metrics reported per arm — BUILD_DOC.md §8."""

from __future__ import annotations

from dataclasses import dataclass

from vasool.bench.harness import InvoiceRunResult
from vasool.domain.money import Money


@dataclass(frozen=True)
class ArmMetrics:
    arm: str
    invoices: int
    recovery_rate: float
    total_recovered: Money
    attempts_per_recovery: float
    mean_days_to_recovery: float | None
    messages_sent: int
    false_dunning_rate: float
    compliance_violations: int


def compute_metrics(arm: str, results: list[InvoiceRunResult]) -> ArmMetrics:
    n = len(results)
    recovered = [r for r in results if r.recovered]

    total_recovered_paise = sum(r.recovered_paise for r in recovered)
    total_attempts = sum(r.attempts_made for r in results)
    messages_sent = sum(r.messages_sent for r in results)
    false_dunning = sum(r.false_dunning_messages for r in results)
    compliance_violations = sum(r.compliance_violations for r in results)

    return ArmMetrics(
        arm=arm,
        invoices=n,
        recovery_rate=len(recovered) / n if n else 0.0,
        total_recovered=Money(total_recovered_paise),
        attempts_per_recovery=(total_attempts / len(recovered)) if recovered else 0.0,
        mean_days_to_recovery=(
            sum(r.days_to_recovery for r in recovered if r.days_to_recovery is not None) / len(recovered)
            if recovered
            else None
        ),
        messages_sent=messages_sent,
        false_dunning_rate=(false_dunning / messages_sent) if messages_sent else 0.0,
        compliance_violations=compliance_violations,
    )
