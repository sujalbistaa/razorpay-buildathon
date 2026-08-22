"""GET /inspector/{invoice_id} — BUILD_PLAN.md Phase 8: "click one invoice, see: the failure
event, the classification and its confidence, the inferred payday with its credible interval,
the downtime state at decision time, every compliance rule with its verdict, the candidate
slots with their EVs, the chosen action, the stop rule, and the outcome."

Everything here is re-derived by calling the same pure policy/compliance functions the
benchmark itself uses, rather than reading a persisted "trace" row -- the whole point of
keeping policy/ and compliance/ pure and side-effect-free is that a decision can be replayed
and fully explained after the fact, not just logged as an opaque outcome.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from vasool.api.seed import SeedData
from vasool.bench.harness import (
    InvoiceRunResult,
    _default_message,
    _invoices_by_customer,
    _payday_evidence_for,
)
from vasool.compliance.constants import MAX_ATTEMPT_WINDOW_DAYS
from vasool.compliance.guard import ComplianceGuard
from vasool.compliance.rules import RuleContext
from vasool.domain.timezones import ist_date, to_ist
from vasool.domain.types import (
    Attempt,
    DowntimeWindow,
    FailureClass,
    Invoice,
    RecoveryPlan,
    RuleResult,
    StopRule,
)
from vasool.policy.base import PolicyContext
from vasool.policy.downtime import DowntimeTracker
from vasool.policy.hazard import HazardFeatures
from vasool.policy.heuristic import ACTION_TABLE, HeuristicAction
from vasool.policy.learned import LearnedPolicy
from vasool.policy.payday import PaydayPosterior
from vasool.policy.planner import build_pair, candidate_debit_times, ev_paise, pair_is_feasible

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_EV_ELIGIBLE_ACTIONS = frozenset(
    {
        HeuristicAction.SILENT_RETRY_AT_PAYDAY,
        HeuristicAction.SILENT_RETRY_ON_DOWNTIME_RESOLVED,
        HeuristicAction.SILENT_RETRY_NEXT_DAY,
        HeuristicAction.RETRY_T3_THEN_CONTACT,
    }
)


@dataclass(frozen=True)
class CandidateTrace:
    debit_at: datetime
    p_success: float
    ev_paise: float


@dataclass(frozen=True)
class AttemptTrace:
    attempt: Attempt
    compliance_results: tuple[RuleResult, ...]
    approved: bool


@dataclass(frozen=True)
class ReasoningTrace:
    invoice: Invoice
    failure_class: FailureClass
    payday_map_estimate: int
    payday_credible_interval: tuple[int, int]
    payday_confidence: float
    payday_n_observations: int
    known_downtime_windows: tuple[DowntimeWindow, ...]
    candidates: tuple[CandidateTrace, ...]
    ev_eligible: bool
    attempts: tuple[AttemptTrace, ...]
    stop_rule: StopRule | None
    outcome: InvoiceRunResult | None
    policy_version: str
    degraded: bool


def build_reasoning_trace(invoice_id: str, seed: SeedData) -> ReasoningTrace | None:
    invoice = next((inv for inv in seed.cohort.invoices if inv.invoice_id == invoice_id), None)
    if invoice is None:
        return None

    customer = seed.cohort.world.customer(invoice.customer_id)
    profile = customer.to_profile()
    failure_class = seed.cohort.origin_failures[invoice.invoice_id]

    by_customer = _invoices_by_customer(seed.cohort)
    siblings = by_customer[invoice.customer_id]
    payday_evidence = _payday_evidence_for(invoice, siblings, seed.cohort)
    posterior = PaydayPosterior.infer(payday_evidence)

    known_downtime = seed.cohort.world.downtime_windows_known_by(invoice.first_failed_at)
    tracker = DowntimeTracker(known_downtime)

    context = PolicyContext(
        customer=profile, failure_class=failure_class, now=invoice.first_failed_at,
        payday_evidence=payday_evidence, known_downtime_windows=known_downtime,
    )

    plan: RecoveryPlan = seed.policy.plan(invoice, context)

    candidates = _candidate_traces(invoice, context, seed, failure_class, tracker, posterior.map_estimate())
    ev_eligible = isinstance(seed.policy, LearnedPolicy) and not seed.policy.degraded and ACTION_TABLE[failure_class] in _EV_ELIGIBLE_ACTIONS

    attempts = _attempt_traces(invoice, context, plan)

    return ReasoningTrace(
        invoice=invoice,
        failure_class=failure_class,
        payday_map_estimate=posterior.map_estimate(),
        payday_credible_interval=posterior.credible_interval(),
        payday_confidence=posterior.confidence(),
        payday_n_observations=posterior.n_observations,
        known_downtime_windows=known_downtime,
        candidates=candidates,
        ev_eligible=ev_eligible,
        attempts=attempts,
        stop_rule=plan.stop_rule,
        outcome=seed.results_by_invoice.get(invoice_id),
        policy_version=seed.policy_version,
        degraded=isinstance(seed.policy, LearnedPolicy) and seed.policy.degraded,
    )


def _candidate_traces(
    invoice: Invoice, context: PolicyContext, seed: SeedData, failure_class: FailureClass,
    tracker: DowntimeTracker, payday_estimate: int,
) -> tuple[CandidateTrace, ...]:
    if not isinstance(seed.policy, LearnedPolicy) or seed.policy.degraded:
        return ()
    if ACTION_TABLE[failure_class] not in _EV_ELIGIBLE_ACTIONS:
        return ()

    # Inspector-only: reads the trained model and costs the policy already holds, to show
    # candidate EVs alongside the plan it produced. Never mutates policy state.
    hazard = seed.policy._hazard
    costs = seed.policy._costs
    assert hazard is not None

    window_end = invoice.first_failed_at + timedelta(days=MAX_ATTEMPT_WINDOW_DAYS)
    traces = []
    for debit_at in candidate_debit_times(invoice.first_failed_at, window_end):
        issuer_down = tracker.is_down(context.customer.issuer, context.customer.mandate_rail, debit_at)
        notice, debit = build_pair(invoice, context, debit_at, start_index=0)
        if not pair_is_feasible(invoice, context, (), notice, debit, issuer_down):
            continue
        features = HazardFeatures(
            failure_class=failure_class,
            days_since_failure=(debit_at - invoice.first_failed_at).total_seconds() / 86400,
            days_relative_to_payday=float(ist_date(debit_at).day - payday_estimate),
            issuer_up=not issuer_down, attempt_index=0, amount=invoice.amount,
            rail=context.customer.mandate_rail, hour=to_ist(debit_at).hour,
        )
        p = hazard.predict(features)
        traces.append(CandidateTrace(debit_at=debit_at, p_success=p, ev_paise=ev_paise(p, invoice.amount, costs)))
    return tuple(traces)


def _attempt_traces(invoice: Invoice, context: PolicyContext, plan: RecoveryPlan) -> tuple[AttemptTrace, ...]:
    ordered = sorted(plan.attempts, key=lambda a: a.debit_at or a.notify_at or invoice.first_failed_at)
    prior: list[Attempt] = []
    notice_delivered_at: datetime | None = None
    traces: list[AttemptTrace] = []
    for attempt in ordered:
        t = attempt.debit_at or attempt.notify_at
        assert t is not None
        message = _default_message(invoice, attempt) if attempt.notify_at is not None else None
        rule_context = RuleContext(
            invoice=invoice, customer=context.customer, failure_class=context.failure_class, now=t,
            prior_attempts=tuple(prior), notice_delivered_at=notice_delivered_at, message=message,
        )
        verdict = ComplianceGuard.evaluate(RecoveryPlan(invoice_id=invoice.invoice_id, attempts=(attempt,)), rule_context)
        traces.append(AttemptTrace(attempt=attempt, compliance_results=verdict.results, approved=verdict.approved))
        prior.append(attempt)
        if attempt.action_type.value == "pre_debit_notice":
            notice_delivered_at = attempt.notify_at
    return tuple(traces)


@router.get("/inspector/{invoice_id}", response_class=HTMLResponse)
def inspect_invoice(request: Request, invoice_id: str) -> HTMLResponse:
    seed: SeedData = request.app.state.seed_data
    trace = build_reasoning_trace(invoice_id, seed)
    if trace is None:
        raise HTTPException(status_code=404, detail=f"invoice {invoice_id!r} not found in seed data")
    return templates.TemplateResponse(request, "inspector.html", {"trace": trace, "invoice_id": invoice_id})


@router.get("/inspector", response_class=HTMLResponse)
def inspector_index(request: Request) -> HTMLResponse:
    seed: SeedData = request.app.state.seed_data
    invoice_ids = [inv.invoice_id for inv in seed.cohort.invoices]
    return templates.TemplateResponse(request, "inspector_index.html", {"invoice_ids": invoice_ids})
