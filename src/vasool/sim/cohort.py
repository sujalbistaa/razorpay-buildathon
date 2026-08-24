"""generate_cohort — one seeded, reproducible batch: customers, invoices and the world they live in."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from vasool.diagnose.classify import classify_failure
from vasool.domain.money import Money
from vasool.domain.types import (
    ActionType,
    Attempt,
    FailureClass,
    FailureEvent,
    Invoice,
    InvoiceCategory,
)
from vasool.llm.client import LLMClient
from vasool.sim.world import (
    CustomerGenerator,
    IssuerAvailability,
    LatentCustomer,
    World,
    load_world_config,
)

# How many synthetic probes to try before giving up on finding a genesis failure for an
# invoice — see _generate_origin_failure. A well-funded, active-mandate customer only fails
# via the final Bernoulli(issuer_base_approval_rate) branch, so exhausting N probes happens
# with probability ~0.85^N; at N=500 that's effectively zero even across a 2,000-invoice batch.
MAX_ORIGIN_FAILURE_PROBES = 500

# Fixed reference instant for the simulated horizon — never datetime.now() (CLAUDE.md invariant 8).
DEFAULT_HORIZON_START = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True)
class Cohort:
    seed: int
    horizon_start: datetime
    horizon_days: int
    customers: tuple[LatentCustomer, ...]
    invoices: tuple[Invoice, ...]
    # invoice_id -> the FailureClass its genesis failure actually resolved to. A Invoice's
    # first_failed_at is just a timestamp; nothing guarantees World.attempt() queried at
    # that exact instant resolves to a failure, so this is computed once at generation time
    # (see _generate_origin_failure) rather than assumed. Shared across every policy arm,
    # same as the rest of the cohort — BUILD_DOC.md §8's "identical latent customer states
    # across all arms" extends to the failure each invoice actually started from.
    origin_failures: dict[str, FailureClass]
    world: World

    def content_hash(self) -> str:
        payload = {
            "seed": self.seed,
            "horizon_start": self.horizon_start.isoformat(),
            "horizon_days": self.horizon_days,
            "customers": [_customer_repr(c) for c in self.customers],
            "invoices": [_invoice_repr(i) for i in self.invoices],
            "origin_failures": {k: v.value for k, v in sorted(self.origin_failures.items())},
        }
        blob = json.dumps(payload, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def split_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {"A": 0, "B": 0}
        for customer in self.customers:
            counts[customer.split] += 1
        return counts


def _customer_repr(customer: LatentCustomer) -> dict[str, object]:
    raw = asdict(customer)
    raw["salary"] = customer.salary.paise
    raw["buffer"] = customer.buffer.paise
    raw["mandate_max_amount"] = customer.mandate_max_amount.paise
    return raw


def _invoice_repr(invoice: Invoice) -> dict[str, object]:
    return {
        "invoice_id": invoice.invoice_id,
        "customer_id": invoice.customer_id,
        "amount_paise": invoice.amount.paise,
        "category": invoice.category.value,
        "first_failed_at": invoice.first_failed_at.isoformat(),
    }


def generate_cohort(
    seed: int,
    n_customers: int,
    n_invoices: int,
    horizon_days: int,
    horizon_start: datetime = DEFAULT_HORIZON_START,
    config: dict[str, Any] | None = None,
    llm_client: LLMClient | None = None,
) -> Cohort:
    # `config` defaults to disk (world.yaml) but bench/robustness.py passes a perturbed copy
    # in-memory -- BUILD_PLAN.md Phase 6's "re-run with world parameters perturbed +/-30-50%"
    # needs a cohort built from a modified config without writing a second YAML file to disk.
    if config is None:
        config = load_world_config()
    # `llm_client` defaults to VASOOL_LLM's own default (stub, deterministic, no network) --
    # invariant 8 holds as long as the caller doesn't opt into VASOOL_LLM=live, at which
    # point cohort generation inherits whatever nondeterminism a live LLM call carries, same
    # as any other opt-in use of the real API.
    if llm_client is None:
        llm_client = LLMClient()
    rng = np.random.default_rng(seed)

    generator = CustomerGenerator(config, rng)
    customers = tuple(generator.generate(f"cust_{i:05d}") for i in range(n_customers))
    customers_by_id = {c.customer_id: c for c in customers}

    issuers = sorted({c.issuer for c in customers})
    issuer_availability = IssuerAvailability(
        config["issuer_availability"], issuers, horizon_start, horizon_days, rng
    )
    world = World(customers_by_id, issuer_availability, config, seed)

    invoices, origin_failures = _generate_invoices(
        rng, customers, n_invoices, horizon_start, horizon_days, config, world, llm_client
    )

    return Cohort(
        seed=seed,
        horizon_start=horizon_start,
        horizon_days=horizon_days,
        customers=customers,
        invoices=invoices,
        origin_failures=origin_failures,
        world=world,
    )


def _generate_origin_failure(world: World, invoice: Invoice, customer: LatentCustomer) -> FailureEvent:
    """The invoice's first_failed_at is a timestamp, not a guarantee. Probe World.attempt()
    at that instant with a synthetic negative attempt_index (never collides with a real
    recovery attempt's index, which starts at 0) until it actually resolves to a failure.
    """
    for probe in range(1, MAX_ORIGIN_FAILURE_PROBES + 1):
        attempt = Attempt(
            invoice_id=invoice.invoice_id,
            attempt_index=-probe,
            action_type=ActionType.SILENT_RETRY,
            rail=customer.mandate_rail,
            amount=invoice.amount,
            notify_at=None,
            debit_at=invoice.first_failed_at,
        )
        outcome = world.attempt(invoice, attempt, invoice.first_failed_at)
        if not outcome.success:
            assert outcome.failure_event is not None
            return outcome.failure_event
    raise RuntimeError(f"no genesis failure found for {invoice.invoice_id} after {MAX_ORIGIN_FAILURE_PROBES} probes")


def _generate_invoices(
    rng: np.random.Generator,
    customers: tuple[LatentCustomer, ...],
    n_invoices: int,
    horizon_start: datetime,
    horizon_days: int,
    config: dict[str, Any],
    world: World,
    llm_client: LLMClient,
) -> tuple[tuple[Invoice, ...], dict[str, FailureClass]]:
    invoice_cfg = config["invoice"]
    amount_cfg = invoice_cfg["amount_inr"]
    category_probs = invoice_cfg["category_probabilities"]
    category_keys = list(category_probs.keys())
    category_weights = np.array([category_probs[k] for k in category_keys], dtype=float)
    category_weights = category_weights / category_weights.sum()

    invoices = []
    origin_failures: dict[str, FailureClass] = {}
    for i in range(n_invoices):
        customer = customers[int(rng.integers(0, len(customers)))]
        amount_rupees = round(float(rng.lognormal(amount_cfg["mean_log"], amount_cfg["sigma_log"])), 2)
        category = InvoiceCategory(category_keys[int(rng.choice(len(category_keys), p=category_weights))])
        offset_days = float(rng.uniform(0, horizon_days))
        invoice = Invoice(
            invoice_id=f"inv_{i:06d}",
            customer_id=customer.customer_id,
            amount=Money.from_rupees(amount_rupees),
            category=category,
            first_failed_at=horizon_start + timedelta(days=offset_days),
        )
        origin_event = _generate_origin_failure(world, invoice, customer)
        invoices.append(invoice)
        # diagnose/rules.py first, diagnose/llm_fallback.py only if that returns no match --
        # BUILD_DOC.md §6's "detection -> diagnosis" happens once here, not per policy arm.
        origin_failures[invoice.invoice_id] = classify_failure(origin_event, llm_client)
    return tuple(invoices), origin_failures
