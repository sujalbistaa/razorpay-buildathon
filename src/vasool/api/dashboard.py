"""GET / — Jinja2 + HTMX + Tailwind CDN + Chart.js, no build step (BUILD_DOC.md §9). At-risk
revenue queue, recovery curves by failure class, the head-to-head benchmark chart, and the
degraded-mode badges (`llm`, `model`, `razorpay`) CLAUDE.md's fallback-everywhere rule requires
surfacing somewhere.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vasool.api.seed import SeedData
from vasool.audit.explain import explain_decision
from vasool.audit.log import AuditLog, DecisionRow
from vasool.domain.money import Money
from vasool.domain.types import ActionType, ComplianceVerdict, Decision, RuleResult
from vasool.execute.razorpay_client import MAX_CONSECUTIVE_FAILURES
from vasool.llm.client import is_stub_mode
from vasool.llm.policy_compiler import PolicyRule, diff_against_default

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

REPO_ROOT = Path(__file__).parent.parent.parent.parent
RESULTS_JSON_PATH = REPO_ROOT / "benchmarks" / "results.json"
QUEUE_PAGE_SIZE = 10


def _load_benchmark_results() -> list[dict[str, Any]]:
    if not RESULTS_JSON_PATH.exists():
        return []
    result: list[dict[str, Any]] = json.loads(RESULTS_JSON_PATH.read_text())
    return result


def _money_on_the_table(benchmark_results: list[dict[str, Any]]) -> dict[str, Any] | None:
    """no_retry vs heuristic only -- both run on the same 2,000-invoice cohort in
    benchmarks/results.json. `learned` is deliberately excluded here: it's evaluated on a
    smaller held-out split (BUILD_DOC.md's held-out discipline), so its absolute rupees
    aren't comparable to no_retry's without normalizing -- recovery_rate is the fair number
    for that comparison, shown in the benchmark chart instead.
    """
    by_arm = {r["arm"]: r for r in benchmark_results}
    baseline = by_arm.get("no_retry")
    ours = by_arm.get("heuristic")
    if baseline is None or ours is None or baseline["invoices"] != ours["invoices"]:
        return None
    delta_paise = ours["total_recovered_paise"] - baseline["total_recovered_paise"]
    return {
        "invoices": ours["invoices"],
        "baseline_inr": Money(baseline["total_recovered_paise"]).format_inr(),
        "ours_inr": Money(ours["total_recovered_paise"]).format_inr(),
        "delta_inr": Money(delta_paise).format_inr(),
    }


def _recovery_waterfall(seed: SeedData) -> dict[str, Any]:
    failed = len(seed.cohort.invoices)
    attempted = 0
    recovered = 0
    recovered_paise = 0
    for invoice in seed.cohort.invoices:
        result = seed.results_by_invoice.get(invoice.invoice_id)
        if result is None:
            continue
        if result.attempts_made > 0 or result.messages_sent > 0:
            attempted += 1
        if result.recovered:
            recovered += 1
            recovered_paise += result.recovered_paise
    return {
        "failed": failed,
        "attempted": attempted,
        "recovered": recovered,
        "still_at_risk": failed - recovered,
        "recovered_inr": Money(recovered_paise).format_inr(),
    }


def _reconstruct_decision(row: DecisionRow) -> Decision:
    """DecisionRow (the SQL row) back into Decision (the domain type) -- exact, not
    approximate: every field DecisionRow stores maps 1:1 back onto Decision's own fields,
    since audit/log.py wrote it from a Decision in the first place.
    """
    results = tuple(
        RuleResult(rule_id=r["rule_id"], passed=r["passed"], reason=r.get("reason"))
        for r in json.loads(row.compliance_results_json)
    )
    # SQLite round-trips a datetime as naive regardless of what was written -- invariant 9
    # ("store UTC") means this is always UTC on the way back out, so we reattach it here
    # rather than let a naive datetime reach Decision's AwareDatetime field.
    decided_at = row.decided_at.replace(tzinfo=UTC)
    return Decision(
        invoice_id=row.invoice_id, attempt_index=row.attempt_index, decided_at=decided_at,
        input_snapshot_hash=row.input_snapshot_hash, policy_version=row.policy_version,
        compliance_verdict=ComplianceVerdict(approved=row.compliance_approved, results=results),
        chosen_action=ActionType(row.chosen_action), expected_value=Money(row.expected_value_paise),
    )


def _policy_rule_summary(rule: PolicyRule | None) -> dict[str, Any] | None:
    if rule is None:
        return None
    return {
        "description": rule.description,
        "min_amount_inr": Money(rule.min_amount_paise).format_inr() if rule.min_amount_paise is not None else "no floor",
        "max_silent_attempts": rule.max_silent_attempts,
    }


def _sample_audit_row(seed: SeedData) -> dict[str, Any] | None:
    row = AuditLog(seed.audit_db_path).sample_decision()
    if row is None:
        return None
    return {
        "invoice_id": row.invoice_id,
        "attempt_index": row.attempt_index,
        "decided_at": row.decided_at,
        "input_snapshot_hash": row.input_snapshot_hash,
        "policy_version": row.policy_version,
        "compliance_approved": row.compliance_approved,
        "chosen_action": row.chosen_action,
        "expected_value_inr": Money(row.expected_value_paise).format_inr(),
    }


def _recovery_by_class(seed: SeedData) -> list[dict[str, Any]]:
    totals: dict[str, int] = defaultdict(int)
    recovered: dict[str, int] = defaultdict(int)
    for invoice in seed.cohort.invoices:
        failure_class = seed.cohort.origin_failures[invoice.invoice_id].value
        totals[failure_class] += 1
        result = seed.results_by_invoice.get(invoice.invoice_id)
        if result is not None and result.recovered:
            recovered[failure_class] += 1
    return [
        {"failure_class": fc, "total": totals[fc], "recovered": recovered[fc], "rate": recovered[fc] / totals[fc]}
        for fc in sorted(totals)
    ]


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, page: int = Query(default=1, ge=1)) -> HTMLResponse:
    seed: SeedData = request.app.state.seed_data
    store = request.app.state.live_store

    open_seed_invoices = [
        {
            "invoice_id": inv.invoice_id,
            "customer_id": inv.customer_id,
            "amount_inr": inv.amount.format_inr(),
            "failure_class": seed.cohort.origin_failures[inv.invoice_id].value,
            "source": "seed",
        }
        for inv in seed.cohort.invoices
        if not (seed.results_by_invoice.get(inv.invoice_id) and seed.results_by_invoice[inv.invoice_id].recovered)
    ]
    live_invoices = [
        {
            "invoice_id": row.invoice_id, "customer_id": row.customer_id,
            "amount_inr": f"₹{row.amount_paise / 100:,.2f}", "failure_class": row.failure_class or "unclassified",
            "source": "live",
        }
        for row in store.list_open_invoices()
    ]
    at_risk_invoices = live_invoices + open_seed_invoices
    at_risk_total_paise = sum(
        inv.amount.paise for inv in seed.cohort.invoices
        if not (seed.results_by_invoice.get(inv.invoice_id) and seed.results_by_invoice[inv.invoice_id].recovered)
    ) + sum(row.amount_paise for row in store.list_open_invoices())

    total_pages = max(1, -(-len(at_risk_invoices) // QUEUE_PAGE_SIZE))
    page = min(page, total_pages)
    start = (page - 1) * QUEUE_PAGE_SIZE
    queue_context = {
        "at_risk_invoices": at_risk_invoices[start : start + QUEUE_PAGE_SIZE],
        "at_risk_page": page,
        "at_risk_total_pages": total_pages,
    }

    # HTMX requests only the queue table + pagination controls swap, not the whole page --
    # keeps state in the URL (hx-push-url) rather than client-side JS, per the server-rendered
    # dashboard rule in CLAUDE.md.
    if request.headers.get("hx-request") == "true":
        return templates.TemplateResponse(request, "_at_risk_queue.html", queue_context)

    state = request.app.state
    demo_razorpay_client = state.demo_chaos_razorpay_client
    razorpay_client = demo_razorpay_client or state.razorpay_client
    # Three distinct LLM states, not two: "stub" (no key configured -- normal, not a fault)
    # and "forced_down" (the fault-injection demo actually broke a real client) render
    # identically as far as is_stub_mode() is concerned, but must read differently to a viewer.
    llm_status = "forced_down" if state.demo_chaos_llm_forced else ("stub" if is_stub_mode() else "live")
    degraded = {
        "llm": llm_status != "live",
        "model": seed.degraded_model or state.demo_chaos_model_forced,
        # None = no real Razorpay account configured (the common `make up` case, seeded data
        # only) -- distinct from a configured or demo-tripped client whose breaker has opened.
        "razorpay": razorpay_client.degraded if razorpay_client is not None else None,
    }
    demo_chaos = {
        "llm": state.demo_chaos_llm_forced,
        "model": state.demo_chaos_model_forced,
        "razorpay": demo_razorpay_client is not None,
    }

    benchmark_results = _load_benchmark_results()

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            **queue_context,
            "at_risk_count": len(at_risk_invoices),
            "at_risk_total_inr": f"₹{at_risk_total_paise / 100:,.2f}",
            "recovery_by_class": _recovery_by_class(seed),
            "recovery_waterfall": _recovery_waterfall(seed),
            "money_on_the_table": _money_on_the_table(benchmark_results),
            "sample_audit_row": _sample_audit_row(seed),
            "explanation": state.demo_last_explanation,
            "benchmark_results": benchmark_results,
            "degraded": degraded,
            "llm_status": llm_status,
            "demo_chaos": demo_chaos,
            "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
            "policy_version": seed.policy_version,
            "downtime_windows": store.list_downtime(),
            "demo_policy_rule": _policy_rule_summary(state.demo_policy_rule),
            "demo_policy_diff": diff_against_default(state.demo_policy_rule) if state.demo_policy_rule else (),
            "demo_policy_fallback_reason": state.demo_policy_fallback_reason,
            "demo_policy_activated": state.demo_policy_activated,
        },
    )


@router.post("/demo/explain")
def explain_sample_decision(request: Request) -> RedirectResponse:
    """Calls the real audit/explain.py path live -- invariant 2's "explains structured
    decisions in English", the one LLM-boundary use this dashboard didn't already show. Uses
    whatever app.state.llm_client currently is, so it honestly reflects the fault-injection
    panel above: forced-down right now -> the real deterministic fallback sentence, not a
    canned one.
    """
    seed: SeedData = request.app.state.seed_data
    row = AuditLog(seed.audit_db_path).sample_decision()
    if row is not None:
        decision = _reconstruct_decision(row)
        request.app.state.demo_last_explanation = explain_decision(decision, request.app.state.llm_client)
    return RedirectResponse(url="/#audit-row", status_code=303)
