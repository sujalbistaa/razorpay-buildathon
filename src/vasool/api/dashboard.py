"""GET / — Jinja2 + HTMX + Tailwind CDN + Chart.js, no build step (BUILD_DOC.md §9). At-risk
revenue queue, recovery curves by failure class, the head-to-head benchmark chart, and the
degraded-mode badges (`llm`, `model`, `razorpay`) CLAUDE.md's fallback-everywhere rule requires
surfacing somewhere.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from vasool.api.seed import SeedData
from vasool.llm.client import is_stub_mode

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

REPO_ROOT = Path(__file__).parent.parent.parent.parent
RESULTS_JSON_PATH = REPO_ROOT / "benchmarks" / "results.json"


def _load_benchmark_results() -> list[dict[str, Any]]:
    if not RESULTS_JSON_PATH.exists():
        return []
    result: list[dict[str, Any]] = json.loads(RESULTS_JSON_PATH.read_text())
    return result


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
def dashboard(request: Request) -> HTMLResponse:
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

    razorpay_client = request.app.state.razorpay_client
    degraded = {
        "llm": is_stub_mode(),
        "model": seed.degraded_model,
        # None = no real Razorpay account configured (the common `make up` case, seeded data
        # only) -- distinct from a configured client whose circuit breaker has opened.
        "razorpay": razorpay_client.degraded if razorpay_client is not None else None,
    }

    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "at_risk_invoices": at_risk_invoices[:50],
            "at_risk_count": len(at_risk_invoices),
            "at_risk_total_inr": f"₹{at_risk_total_paise / 100:,.2f}",
            "recovery_by_class": _recovery_by_class(seed),
            "benchmark_results": _load_benchmark_results(),
            "degraded": degraded,
            "policy_version": seed.policy_version,
            "downtime_windows": store.list_downtime(),
        },
    )
