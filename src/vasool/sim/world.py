"""The causal generative world. Its state is hidden from policy — only FailureClass and the
Razorpay-shaped error payload cross that boundary (BUILD_DOC.md §3).

World.attempt() is a pure function of (customer latent state, issuer downtime schedule,
config, t): nothing it touches is mutated by a query. That's deliberate — two policy arms
querying the same (invoice, action, t) must see the same outcome regardless of what other
attempts either arm has made or in what order, or the head-to-head benchmark isn't honest.
The stochastic pieces (the final approval roll, the velocity check) are derived from a hash
of (seed, invoice_id, attempt_index) rather than drawn from a shared mutable RNG, for the
same reason. snapshot()/restore() exist per BUILD_PLAN.md Phase 3 but are near-trivial here,
since nothing in Phase 3 mutates a World after construction; Phase 4's harness is where real
per-run state (e.g. "has this invoice been paid") will live and actually need them.
"""

from __future__ import annotations

import calendar
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from vasool.compliance.constants import (
    AFA_ELEVATED_CATEGORIES,
    AFA_FREE_ELEVATED_LIMIT,
    AFA_FREE_LIMIT,
)
from vasool.domain.money import Money
from vasool.domain.taxonomy import DEFAULT_SOURCE
from vasool.domain.timezones import ist_date, to_ist
from vasool.domain.types import (
    ActionType,
    Attempt,
    CustomerProfile,
    FailureClass,
    FailureEvent,
    Invoice,
    MandateState,
    Rail,
    Severity,
)

WORLD_YAML_PATH = Path(__file__).parent / "world.yaml"


def load_world_config(path: Path = WORLD_YAML_PATH) -> dict[str, Any]:
    with path.open() as f:
        config: dict[str, Any] = yaml.safe_load(f)
    return config


class CardState(StrEnum):
    """Hidden from the policy entirely — only ever surfaces as CARD_EXPIRED / _BLOCKED classes."""

    VALID = "valid"
    EXPIRED = "expired"
    BLOCKED = "blocked"
    REISSUED = "reissued"


def _digest_unit_interval(*parts: object) -> float:
    """Deterministic uniform draw in [0, 1) from a hash of `parts` — no shared RNG state."""
    key = ":".join(str(p) for p in parts)
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    as_int = int.from_bytes(digest[:8], "big")
    return as_int / 2**64


def stable_split(customer_id: str) -> str:
    """Deterministic A/B assignment, independent of the RNG stream — BUILD_PLAN.md Phase 3."""
    return "A" if _digest_unit_interval("split", customer_id) < 0.5 else "B"


def _sample_categorical(rng: np.random.Generator, probabilities: dict[str, float]) -> str:
    keys = list(probabilities.keys())
    weights = np.array([probabilities[k] for k in keys], dtype=float)
    weights = weights / weights.sum()
    choice = rng.choice(len(keys), p=weights)
    return keys[int(choice)]


def _sample_payday_dom(rng: np.random.Generator, mixture: list[dict[str, Any]]) -> int:
    weights = np.array([component["weight"] for component in mixture], dtype=float)
    weights = weights / weights.sum()
    component = mixture[int(rng.choice(len(mixture), p=weights))]
    if component["kind"] == "last_working_day":
        return 30  # BalanceProcess clamps to the real last day of whatever month it's in
    spread_days = int(component["spread_days"])
    offset = int(rng.integers(-spread_days, spread_days + 1))
    return max(1, min(28, int(component["day"]) + offset))


def _sample_lognormal_paise(rng: np.random.Generator, cfg: dict[str, float]) -> Money:
    rupees = float(rng.lognormal(cfg["mean_log"], cfg["sigma_log"]))
    return Money.from_rupees(round(rupees, 2))


def _sample_exponential_paise(rng: np.random.Generator, cfg: dict[str, float]) -> Money:
    rupees = float(rng.exponential(cfg["scale"]))
    return Money.from_rupees(round(rupees, 2))


def _sample_beta(rng: np.random.Generator, cfg: dict[str, float]) -> float:
    return float(rng.beta(cfg["alpha"], cfg["beta"]))


@dataclass(frozen=True)
class LatentCustomer:
    """The simulator's ground truth. Never exposed to policy directly — see to_profile()."""

    customer_id: str
    split: str
    language: str
    payday_dom: int
    salary: Money
    spend_rate: float
    buffer: Money
    card_state: CardState
    mandate_rail: Rail
    mandate_state: MandateState
    mandate_max_amount: Money
    issuer: str
    base_response_rate: float
    fatigue_decay: float
    intent_to_pay: bool

    def to_profile(self) -> CustomerProfile:
        return CustomerProfile(
            customer_id=self.customer_id,
            split=self.split,  # type: ignore[arg-type]
            language=self.language,  # type: ignore[arg-type]
            mandate_rail=self.mandate_rail,
            mandate_state=self.mandate_state,
            mandate_max_amount=self.mandate_max_amount,
            issuer=self.issuer,
            opted_out=False,
            promise_to_pay_until=None,
        )


class CustomerGenerator:
    def __init__(self, config: dict[str, Any], rng: np.random.Generator) -> None:
        self._cfg = config["customer"]
        self._rng = rng

    def generate(self, customer_id: str) -> LatentCustomer:
        rng, cfg = self._rng, self._cfg
        card_state = CardState(_sample_categorical(rng, cfg["card"]["state_probabilities"]))
        mandate_rail = Rail(_sample_categorical(rng, cfg["mandate"]["rail_probabilities"]))
        mandate_state = MandateState(_sample_categorical(rng, cfg["mandate"]["state_probabilities"]))
        return LatentCustomer(
            customer_id=customer_id,
            split=stable_split(customer_id),
            language=_sample_categorical(rng, cfg["language_probabilities"]),
            payday_dom=_sample_payday_dom(rng, cfg["payday_dom"]["mixture"]),
            salary=_sample_lognormal_paise(rng, cfg["balance"]["salary_inr"]),
            spend_rate=_sample_beta(rng, cfg["balance"]["spend_rate"]),
            buffer=_sample_exponential_paise(rng, cfg["balance"]["buffer_inr"]),
            card_state=card_state,
            mandate_rail=mandate_rail,
            mandate_state=mandate_state,
            mandate_max_amount=_sample_lognormal_paise(rng, cfg["mandate"]["max_amount_inr"]),
            issuer=str(rng.choice(cfg["issuer_pool"])),
            base_response_rate=_sample_beta(rng, cfg["engagement"]["base_response_rate"]),
            fatigue_decay=float(cfg["engagement"]["fatigue_decay"]),
            intent_to_pay=bool(rng.random() < cfg["intent_to_pay_rate"]),
        )


class BalanceProcess:
    """balance(t): jumps to `salary` at payday, decays linearly at spend_rate, floors at buffer.

    A customer who never intends to pay (LatentCustomer.intent_to_pay is False) simply never
    has money: not a special-cased override branch, but the same INSUFFICIENT_FUNDS path
    every other underfunded customer takes — "never recoverable" falls out causally instead
    of needing its own resolution rule that BUILD_DOC.md §3.3 doesn't actually list.
    """

    @staticmethod
    def balance_at(customer: LatentCustomer, t: datetime) -> Money:
        if not customer.intent_to_pay:
            return Money(0)
        days_since_payday = BalanceProcess._days_since_last_payday(customer.payday_dom, t)
        burned = customer.salary.paise * customer.spend_rate * days_since_payday
        remaining = round(customer.salary.paise - burned)
        return Money(max(customer.buffer.paise, remaining))

    @staticmethod
    def _days_since_last_payday(payday_dom: int, t: datetime) -> float:
        today = ist_date(t)
        this_month_payday = _clamp_to_month(today.year, today.month, payday_dom)
        if this_month_payday <= today:
            payday = this_month_payday
        else:
            prev_year, prev_month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
            payday = _clamp_to_month(prev_year, prev_month, payday_dom)
        elapsed_days = (today - payday).days
        ist_now = to_ist(t)
        fractional_day = ist_now.hour / 24 + ist_now.minute / 1440 + ist_now.second / 86400
        return elapsed_days + fractional_day


def _clamp_to_month(year: int, month: int, day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last_day))


@dataclass(frozen=True)
class DowntimeWindow:
    issuer: str
    method: Rail
    severity: Severity
    begin: datetime
    end: datetime
    scheduled: bool = False


class IssuerAvailability:
    """Poisson-arrival downtime bursts, log-normal durations, month-end congestion multiplier.

    The full schedule is precomputed once at construction from the seeded rng, then only ever
    read — so every policy arm queries the identical downtime calendar (BUILD_DOC.md §3.2).
    """

    def __init__(
        self,
        config: dict[str, Any],
        issuers: list[str],
        horizon_start: datetime,
        horizon_days: int,
        rng: np.random.Generator,
    ) -> None:
        self._windows = self._generate(config, issuers, horizon_start, horizon_days, rng)

    @classmethod
    def from_windows(cls, windows: tuple[DowntimeWindow, ...]) -> IssuerAvailability:
        """Build directly from known windows — real downtime data in live mode, or a fixture in tests."""
        instance = cls.__new__(cls)
        instance._windows = windows
        return instance

    def _generate(
        self,
        config: dict[str, Any],
        issuers: list[str],
        horizon_start: datetime,
        horizon_days: int,
        rng: np.random.Generator,
    ) -> tuple[DowntimeWindow, ...]:
        windows: list[DowntimeWindow] = []
        base_rate = config["downtime_arrivals_per_day"]
        month_end_days = set(config["month_end_days"])
        multiplier = config["month_end_congestion_multiplier"]
        duration_cfg = config["duration_hours"]
        for issuer in issuers:
            for method in (Rail.CARD, Rail.UPI_AUTOPAY, Rail.ENACH):
                t = 0.0
                while t < horizon_days:
                    current_day = (horizon_start + timedelta(days=t)).day
                    rate = base_rate * (multiplier if current_day in month_end_days else 1.0)
                    t += float(rng.exponential(1.0 / rate))
                    if t >= horizon_days:
                        break
                    begin = horizon_start + timedelta(days=t)
                    duration_hours = float(rng.lognormal(duration_cfg["mean_log"], duration_cfg["sigma_log"]))
                    end = begin + timedelta(hours=duration_hours)
                    severity = Severity(_sample_categorical(rng, config["severity_probabilities"]))
                    windows.append(
                        DowntimeWindow(issuer=issuer, method=method, severity=severity, begin=begin, end=end)
                    )
        return tuple(sorted(windows, key=lambda w: w.begin))

    def status_at(self, issuer: str, method: Rail, t: datetime) -> DowntimeWindow | None:
        for window in self._windows:
            if window.issuer == issuer and window.method == method and window.begin <= t < window.end:
                return window
        return None

    def is_down(self, issuer: str, method: Rail, t: datetime) -> bool:
        window = self.status_at(issuer, method, t)
        return window is not None and window.severity is Severity.HIGH

    @property
    def windows(self) -> tuple[DowntimeWindow, ...]:
        return self._windows


@dataclass(frozen=True)
class AttemptOutcome:
    success: bool
    failure_event: FailureEvent | None


class World:
    def __init__(
        self,
        customers: dict[str, LatentCustomer],
        issuer_availability: IssuerAvailability,
        config: dict[str, Any],
        seed: int,
    ) -> None:
        self._customers = customers
        self._issuer_availability = issuer_availability
        self._config = config
        self._seed = seed

    def customer(self, customer_id: str) -> LatentCustomer:
        return self._customers[customer_id]

    def snapshot(self) -> World:
        # Nothing here is mutated by attempt() — see the module docstring. A World is already
        # its own snapshot; this exists so Phase 4's harness has a stable point to restore to
        # once it adds real per-run state on top of World.
        return self

    def restore(self, snapshot: World) -> World:
        return snapshot

    def attempt(self, invoice: Invoice, action: Attempt, t: datetime) -> AttemptOutcome:
        customer = self._customers[invoice.customer_id]

        def fail(failure_class: FailureClass, description: str) -> AttemptOutcome:
            event = FailureEvent(
                invoice_id=invoice.invoice_id,
                code=failure_class.value.upper(),
                description=description,
                source=DEFAULT_SOURCE[failure_class],
                step="payment_authorization",
                reason=failure_class.value,
                occurred_at=t,
            )
            return AttemptOutcome(success=False, failure_event=event)

        if customer.mandate_state is MandateState.REVOKED:
            return fail(FailureClass.MANDATE_REVOKED, "mandate revoked at the customer's bank")
        if customer.mandate_state is MandateState.PAUSED:
            return fail(FailureClass.MANDATE_PAUSED, "mandate paused at the customer's bank")
        if customer.card_state is CardState.EXPIRED:
            return fail(FailureClass.CARD_EXPIRED, "card expired")
        if customer.card_state is CardState.BLOCKED:
            return fail(FailureClass.DEBIT_INSTRUMENT_BLOCKED, "card blocked")

        if self._issuer_availability.is_down(customer.issuer, action.rail, t):
            if action.rail is Rail.CARD:
                return fail(FailureClass.GATEWAY_TECHNICAL_ERROR, f"{customer.issuer} gateway down")
            return fail(FailureClass.REMITTER_BANK_DOWN, f"{customer.issuer} remitter bank down")

        if action.amount.paise > customer.mandate_max_amount.paise:
            return fail(FailureClass.TRANSACTION_LIMIT_EXCEEDED, "amount exceeds registered mandate cap")

        if action.action_type is ActionType.SILENT_RETRY:
            afa_limit = AFA_FREE_ELEVATED_LIMIT if invoice.category in AFA_ELEVATED_CATEGORIES else AFA_FREE_LIMIT
            if action.amount.paise > afa_limit.paise:
                return fail(FailureClass.AFA_REQUIRED, "above the AFA-free ceiling, no AFA on a silent retry")

        if _digest_unit_interval(self._seed, invoice.invoice_id, action.attempt_index, "velocity") < (
            self._config["bank_side_limits"]["velocity_exceeded_rate"]
        ):
            return fail(FailureClass.VELOCITY_EXCEEDED, "velocity limit exceeded at customer's bank")

        if BalanceProcess.balance_at(customer, t).paise < action.amount.paise:
            return fail(FailureClass.INSUFFICIENT_FUNDS, "insufficient balance at attempt time")

        approval_roll = _digest_unit_interval(self._seed, invoice.invoice_id, action.attempt_index, "approval")
        if approval_roll < self._config["issuer_availability"]["issuer_base_approval_rate"]:
            return AttemptOutcome(success=True, failure_event=None)

        generic_decline = FailureClass.CARD_DECLINED if action.rail is Rail.CARD else FailureClass.DEBIT_FAILED
        return fail(generic_decline, "declined, no more specific reason available")
