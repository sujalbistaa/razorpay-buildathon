"""Writes benchmarks/results.json and benchmarks/report.md from a list of ArmMetrics.

Both are committed deliberately (CLAUDE.md's Git section) so a reviewer sees the numbers
without running anything — never write a number here that didn't come out of this module.
"""

from __future__ import annotations

import json
from pathlib import Path

from vasool.bench.metrics import ArmMetrics

COLUMNS = [
    "arm",
    "recovery_rate",
    "total_recovered",
    "attempts_per_recovery",
    "mean_days_to_recovery",
    "messages_sent",
    "false_dunning_rate",
    "compliance_violations",
]


def write_results_json(metrics: list[ArmMetrics], path: Path) -> None:
    payload = [
        {
            "arm": m.arm,
            "invoices": m.invoices,
            "recovery_rate": m.recovery_rate,
            "total_recovered_paise": m.total_recovered.paise,
            "attempts_per_recovery": m.attempts_per_recovery,
            "mean_days_to_recovery": m.mean_days_to_recovery,
            "messages_sent": m.messages_sent,
            "false_dunning_rate": m.false_dunning_rate,
            "compliance_violations": m.compliance_violations,
        }
        for m in metrics
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_report_md(metrics: list[ArmMetrics], path: Path) -> None:
    header = "| arm | invoices | recovery rate | recovered | attempts/recovery | mean days | messages | false dunning | violations |"
    sep = "|---|---|---|---|---|---|---|---|---|"
    rows = []
    for m in metrics:
        mean_days = f"{m.mean_days_to_recovery:.1f}" if m.mean_days_to_recovery is not None else "—"
        rows.append(
            f"| {m.arm} | {m.invoices} | {m.recovery_rate:.1%} | {m.total_recovered.format_inr()} | "
            f"{m.attempts_per_recovery:.2f} | {mean_days} | {m.messages_sent} | "
            f"{m.false_dunning_rate:.1%} | {m.compliance_violations} |"
        )
    note = (
        "\n`invoices` differs across arms: baselines and `heuristic` run over the full "
        "2,000-invoice cohort (A+B); `learned` is scored on held-out cohort B only "
        "(BUILD_PLAN.md Phase 6) -- its `recovered` total is not directly comparable to the "
        "other arms' totals for that reason, though `recovery rate` still is. See the "
        "paired, same-population comparison below.\n"
    )
    content = "# Benchmark report\n\n" + "\n".join([header, sep, *rows]) + "\n" + note
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
