# BUILD_PLAN.md — work order

Read `CLAUDE.md` first, then `BUILD_DOC.md`, then this.

Execute one phase at a time. At the end of each phase, run the acceptance check, print the real output, and **stop for review before starting the next phase**. Do not run ahead. Do not build Phase 5 machinery while doing Phase 2.

Phase 4 is the gate. Nothing after it matters if Phase 4 doesn't produce a number.

---

## Phase 0 — Skeleton

**Build**

- `pyproject.toml` (Python 3.11), `Makefile`, `docker-compose.yml`, `.env.example`, `.gitignore`
- Package tree exactly as in `BUILD_DOC.md` §9, every package with `__init__.py` and a one-line docstring
- `src/vasool/domain/money.py` — `Money` value type wrapping `int` paise: `from_rupees`, `to_rupees`, `__add__`, `__sub__`, `__mul__` (int only), `format_inr`. Rejects float construction.
- `src/vasool/domain/clock.py` — `Clock` Protocol with `now() -> datetime` (tz-aware UTC), plus `SystemClock` and `FrozenClock`
- `src/vasool/domain/timezones.py` — `IST = ZoneInfo("Asia/Kolkata")`, `to_ist`, `ist_date`, `day_of_month_ist`
- `structlog` JSON config in `src/vasool/logging.py`
- Makefile targets: `install`, `test`, `lint`, `up`, `seed`, `bench`, `chaos`, `live` (stubs that print "not implemented" for the ones not built yet)

**Accept**

```
make install && make test && make lint
```
green, and `python -c "from vasool.domain.money import Money; print(Money.from_rupees(1234.56).format_inr())"` prints `₹1,234.56`. Adding a float to a `Money` raises.

---

## Phase 1 — Domain types and failure taxonomy

**Build**

`src/vasool/domain/types.py`:

- `FailureClass(StrEnum)` — the complete taxonomy. Card reasons from Razorpay docs: `INSUFFICIENT_FUNDS`, `CARD_EXPIRED`, `CARD_DECLINED`, `DEBIT_INSTRUMENT_BLOCKED`, `DEBIT_INSTRUMENT_INACTIVE`, `CARD_NOT_ENROLLED`, `AUTHENTICATION_FAILED`, `INCORRECT_CVV`, `PAYMENT_RISK_CHECK_FAILED`, `TRANSACTION_LIMIT_EXCEEDED`, `GATEWAY_TECHNICAL_ERROR`, `BANK_TECHNICAL_ERROR`, `PAYMENT_TIMED_OUT`, `PAYMENT_CANCELLED`. UPI/NPCI: `VELOCITY_EXCEEDED` (Z7), `PER_TXN_LIMIT_EXCEEDED` (Z8), `COLLECT_EXPIRED` (U69), `DEBIT_FAILED` (U30), `REMITTER_BANK_DOWN` (U28). Mandate: `MANDATE_REVOKED`, `MANDATE_PAUSED`, `AFA_REQUIRED`. Terminal: `UNKNOWN`.
- `FailureSource(StrEnum)`: `CUSTOMER`, `BUSINESS`, `RAZORPAY`, `GATEWAY`, `BANK`, `NETWORK`
- `Severity`, `Rail(StrEnum)` = `UPI_AUTOPAY | ENACH | CARD`, `MandateState`, `ActionType` = `SILENT_RETRY | PRE_DEBIT_NOTICE | CONTACT_LINK | CREDENTIAL_UPDATE_REQUEST | STOP`
- `FailureEvent`, `CustomerProfile`, `Invoice`, `Attempt`, `RecoveryPlan`, `StopRule`, `Decision`, `ComplianceVerdict` — all Pydantic v2, all frozen where they represent facts
- `CustomerProfile.split: Literal["A", "B"]` — assigned once at cohort generation (Phase 3), never reassigned. Cohort A trains the learned policy's exploration log; cohort B is held out for evaluation (BUILD_DOC.md §8). Carrying the field from Phase 1 means Phase 4's full-cohort benchmark and Phase 6's train/eval split read the same data without a schema change in between.

`src/vasool/domain/taxonomy.py`:

- `RECOVERABILITY: dict[FailureClass, Recoverability]` where `Recoverability` is `SOFT | AMBIGUOUS | HARD`
- `DEFAULT_SOURCE: dict[FailureClass, FailureSource]` — this is what drives the false-dunning metric later
- `HARD_DECLINE_CLASSES: frozenset[FailureClass]`

Every mapping entry gets a source comment pointing at the Razorpay docs URL it came from.

**Accept**

`tests/test_taxonomy.py` asserts every `FailureClass` member appears in both dicts, and that `HARD_DECLINE_CLASSES` is exactly the set with `Recoverability.HARD`. No enum member is unclassified.

---

## Phase 2 — Compliance engine

This is the module a payments engineer will read first. Make it the best-written code in the repo.

**Build**

`src/vasool/compliance/constants.py` — every constant from `BUILD_DOC.md` §5.1, each with a source comment carrying the regulation name and URL.

`src/vasool/compliance/rules.py` — one function per rule, each with signature `(attempt, context) -> RuleResult`, each with a stable `rule_id` string and a human-readable reason on rejection:

- `R001_PRE_DEBIT_NOTICE` — any mandate debit must have a delivered notice ≥ 24h before `debit_at`
- `R002_AFA_THRESHOLD` — amount above the applicable AFA-free limit requires AFA; the elevated ₹1,00,000 limit applies only to `{INSURANCE, MUTUAL_FUND, CREDIT_CARD_BILL}` categories
- `R003_MAX_SILENT_ATTEMPTS`
- `R004_ATTEMPT_WINDOW`
- `R005_MIN_INTERVAL_SAME_PATH`
- `R006_HARD_DECLINE_NO_RETRY`
- `R007_MANDATE_ACTIVE`
- `R008_MANDATE_CAP` — amount must not exceed `mandate.max_amount`
- `R009_CONTACT_QUIET_HOURS` — evaluated in IST
- `R010_MESSAGE_FREQUENCY` — max 3 per invoice, min 48h apart
- `R011_ISSUER_RATE_LIMIT` — token bucket per issuer
- `R012_CUSTOMER_OPT_OUT`
- `R013_PROMISE_TO_PAY_SUPPRESSION`
- `R014_MESSAGE_CONTENT` — required fields present: merchant name, exact amount, debit date, opt-out
- `R015_ISSUER_DOWNTIME_GATE` — a mandate debit is not eligible while the relevant `(method, issuer)` is at `severity: high` and unresolved per the downtime event stream. This replaces `downtime_penalty` from an earlier draft of the EV formula (BUILD_DOC.md §4.3): a gate, not a cost — the planner must not be able to buy through a known outage.

`src/vasool/compliance/guard.py` — `ComplianceGuard.evaluate(plan, context) -> ApprovedPlan | Rejection`. Runs every rule, collects every result (not short-circuit — the audit trail wants the full evaluation), returns approved only if all pass.

`src/vasool/compliance/buckets.py` — per-issuer token bucket, injectable clock.

**Accept**

`tests/test_compliance.py`, table-driven, at least three cases per rule (pass / fail / boundary). Boundary cases must include exactly 24.0h notice, exactly ₹15,000, exactly ₹15,000.01, exactly ₹1,00,000 for an elevated category and for a non-elevated one, and 21:00:00 IST sharp. Every `rule_id` appears in at least one passing and one failing case.

---

## Phase 3 — Simulator

**Build**

`src/vasool/sim/world.yaml` — every parameter from `BUILD_DOC.md` §3, each line commented `# SOURCED: <url>` or `# ESTIMATED: <one-line reasoning>`. Also carries the two EV cost terms consumed by the Phase 6 planner: `notification_cost_paise` (`ESTIMATED`, per BUILD_DOC.md §4.3) and `annoyance_cost_paise` (commented `# POLICY PARAMETER, NOT ESTIMATED` — no reasoning line, because there is none to give).

`src/vasool/sim/world.py`:

- `CustomerGenerator` — samples latent state per §3.1 from a seeded `numpy.random.Generator`
- `BalanceProcess` — `balance(t)` jumps at `payday_dom`, decays at `spend_rate`, floors at `buffer`
- `IssuerAvailability` — Poisson downtime bursts, log-normal durations, month-end congestion multiplier; emits events matching the real Razorpay `payment.downtime.*` schema (`method`, `instrument.issuer`, `instrument.card_type`, `severity`, `scheduled`, `begin`, `end`)
- `World.attempt(invoice, action, t) -> AttemptOutcome` — resolves in exactly the order in §3.3, returns a `FailureClass` and a realistic error payload shaped like a real Razorpay error object
- `World.snapshot()` / `World.restore()` so every policy arm runs against an identical world

`src/vasool/sim/cohort.py` — `generate_cohort(seed, n_customers, n_invoices, horizon_days) -> Cohort`. Assigns `CustomerProfile.split` deterministically by `stable_hash(customer_id) % 2`, not by RNG draw — the assignment must stay fixed regardless of what else consumes the seeded generator's stream, so Phase 6 reading the split field later can't perturb the Phase 3 determinism hash.

`ASSUMPTIONS.md` at repo root — the human-readable version of `world.yaml`, split into **Sourced** (with URLs) and **Estimated** (with reasoning), plus a closing paragraph on what this means for interpreting the benchmark.

**Accept**

```
make seed
```
generates 500 customers / 2,000 invoices reproducibly. `tests/test_determinism.py` asserts two runs with the same seed produce identical cohort hashes, and that the `A`/`B` split is ~50/50 and identical byte-for-byte across runs. A quick histogram script shows the failure-class mix is plausible (soft declines dominate; hard declines are a minority).

---

## Phase 4 — Baselines and the benchmark harness · **GATE**

Nothing after this phase matters until this phase produces a number.

**Build**

`src/vasool/policy/base.py` — `Policy` Protocol: `plan(invoice, context) -> RecoveryPlan`

`src/vasool/policy/baselines.py`:

- `NoRetryPolicy`
- `RazorpayDefaultPolicy` — T+1, T+2, T+3 daily, then halt. Docstring cites https://razorpay.com/docs/payments/subscriptions/payment-retries/
- `Static137Policy`
- `DunningOnlyPolicy`

`src/vasool/execute/protocol.py` — `Executor` Protocol
`src/vasool/execute/simulator_client.py` — implements it against `World`

`src/vasool/audit/log.py` — append-only decision log with `record_decision()` and `record_outcome()`; SQLite table, no updates to decision rows

`src/vasool/bench/harness.py` — runs an arm over a cohort with a frozen world snapshot, driving the scheduler forward in simulated time. Baselines and `heuristic` are stateless and run over the full cohort (splits `A`+`B` combined); the `split` field only starts mattering in Phase 6, where `learned` trains on `A`'s exploration log and reports only on `B`.
`src/vasool/bench/metrics.py` — recovery rate, ₹ recovered, attempts per recovery, mean days to recovery, messages sent, **false dunning rate**, compliance violations
`src/vasool/bench/report.py` — writes `benchmarks/results.json` and `benchmarks/report.md`

**Accept**

```
make bench
```
prints a table of all four baselines with all seven metrics, and writes both artefacts. `razorpay_default` must beat `no_retry` and `dunning_only` must have a nonzero false dunning rate — if either fails, the simulator is wrong, not the metric.

**Stop here and report the actual numbers before continuing.**

---

## Phase 5 — Heuristic policy, payday inference, downtime gating

**Build**

`src/vasool/policy/payday.py` — `PaydayPosterior`: per-customer posterior over day-of-month from historical successful debit dates plus observed `INSUFFICIENT_FUNDS` dates, shrunk toward a population prior. Exposes `map_estimate()`, `credible_interval()`, and `confidence()`. Customers below a confidence threshold are flagged so the planner falls back rather than pretending.

`src/vasool/policy/downtime.py` — tracks open downtime windows from the event stream; `is_down(issuer, method, t)`, `expected_resolution(issuer, t)`.

`src/vasool/policy/heuristic.py` — the decision table from `BUILD_DOC.md` §4.2. Every attempt is a `(notify_at, debit_at)` pair on mandate rails, so the 24h notice constraint is satisfied by construction rather than by rejection.

`src/vasool/bench/plots.py` — inferred vs true payday scatter (pulling the simulator's hidden `payday_dom`), and the population histogram of chosen debit days-of-month.

**Accept**

`make bench` now includes `heuristic` and it beats `razorpay_default` on ₹ recovered. `benchmarks/payday_inference.png` exists and shows correlation. The population histogram concentrates on the 3rd–7th, which independently reproduces Razorpay's published guidance — note this explicitly in `benchmarks/report.md`.

---

## Phase 6 — Learned policy

**Build**

`src/vasool/policy/hazard.py` — discrete-time hazard model. Features per `BUILD_DOC.md` §4.3. LightGBM binary classifier over `(attempt_context, slot) -> success`. Train on cohort A's exploration log; **split by customer, never by attempt**.

`src/vasool/policy/planner.py` — EV computation (`P(success) × amount − notification_cost − annoyance_cost`, both from `world.yaml`; downtime is not a term here, it's `R015` in `ComplianceGuard` — see BUILD_DOC.md §4.3) and greedy-with-depth-2-lookahead over the compliance-feasible action set. `StopRule` evaluation per §4.4.

`src/vasool/policy/explore.py` — Thompson sampling over `failure_class × time-bucket` cells, applied only where hazard-model uncertainty exceeds a threshold. Module docstring states plainly: posteriors update on observed outcomes only, no LLM output enters the posterior, arm choice, or reward — this is the system using probability, not the LLM computing it (CLAUDE.md invariant 2).

`src/vasool/policy/learned.py` — assembles the above. **On missing or corrupt model artefact, falls back to `HeuristicPolicy`, logs a loud warning, sets `degraded: model`, and keeps serving.**

`src/vasool/bench/ablation.py` — reason-awareness → +payday → +downtime gating → +EV stopping → +dunning, as a stacked contribution bar.

`src/vasool/bench/robustness.py` — re-runs the full benchmark with world parameters perturbed ±30% and ±50% (payday distribution shifted, downtime rate doubled, hard-decline mix tripled, engagement halved) and tabulates the lift under each.

**Accept**

`make bench` reports `learned` on held-out cohort B with a paired bootstrap 95% CI on the difference vs `razorpay_default`. `benchmarks/ablation.png` and `benchmarks/robustness.md` exist. The robustness table is reported honestly including the cases where the lift shrinks.

---

## Phase 7 — LLM layer

Re-read invariant 2 in `CLAUDE.md` before starting.

**Build**

`src/vasool/llm/client.py` — Anthropic client, strict JSON output, timeout, single retry, and a `FallbackTriggered` path on any failure. Never raises to callers.

`src/vasool/diagnose/rules.py` — deterministic error-string → `FailureClass` table. Primary path.
`src/vasool/diagnose/llm_fallback.py` — only for strings the table misses. Output constrained to the enum, with `confidence` and `evidence`. Below threshold → `UNKNOWN`.

`src/vasool/comms/generate.py` — message generation, language selected from the customer profile (`en` / `hi` / `hinglish`), tone selected from `FailureClass`.
`src/vasool/comms/validate.py` — **mandatory** pre-send validator: amount / debit date / merchant name must match canonical record values by exact string containment; opt-out present; length caps per channel; no threats, no legal claims, no invented dates. Failure → deterministic template, log `llm_validation_failed`, continue.
`src/vasool/comms/templates.py` — the deterministic fallback for every `FailureClass` × language.

`src/vasool/llm/narrative.py` — batch root-cause summary for the merchant.
`src/vasool/llm/policy_compiler.py` — natural language → typed `PolicyRule` JSON → validate → diff against live policy → **require explicit confirmation before activation**. Never auto-applies.
`src/vasool/audit/explain.py` — structured `Decision` → one English sentence. Derived, never authoritative.

**Accept**

`tests/test_comms_validation.py` feeds deliberately bad generations — wrong amount, hallucinated date, missing opt-out, a threat — and asserts each is rejected and the template is used. `tests/test_llm_fallback.py` asserts that with the LLM client raising on every call, the full benchmark still runs and produces the same recovery numbers.

---

## Phase 8 — API, dashboard, decision inspector

**Build**

`src/vasool/api/webhooks.py`:
- `POST /webhooks/razorpay` — read **raw** body, verify `X-Razorpay-Signature` as HMAC-SHA256 hex against the webhook secret in constant time, dedupe on `x-razorpay-event-id`, **ack within 200ms**, enqueue. Signature mismatch → 400, structured log, zero state change.
- Handles `payment.failed`, `subscription.pending`, `subscription.halted`, `subscription.charged`, `invoice.paid`, `payment_link.paid`, `payment.downtime.started|updated|resolved`

`src/vasool/api/dashboard.py` — Jinja2 + HTMX + Tailwind CDN + Chart.js, no build step:
- at-risk revenue queue with live totals
- recovery curves by failure class
- head-to-head benchmark chart
- degraded-mode badges (`llm`, `model`, `razorpay`)

`src/vasool/api/inspector.py` — click one invoice, see: the failure event, the classification and its confidence, the inferred payday with its credible interval, the downtime state at decision time, every compliance rule with its verdict, the candidate slots with their EVs, the chosen action, the stop rule, and the outcome.

`src/vasool/api/admin.py` — `/admin/dlq` and `/admin/dlq/replay`.

**Accept**

`make up` → dashboard at `localhost:8000` with seeded data. The inspector renders a full reasoning chain for any invoice. A webhook with a bad signature returns 400 and changes nothing; the same webhook delivered twice causes exactly one state transition.

---

## Phase 9 — Live Razorpay loop and chaos mode

**Build**

`src/vasool/execute/razorpay_client.py` — implements `Executor` against real test-mode APIs. Create Payment Link (standard and `upi_link: true`), fetch downtime, fetch payment. Idempotency key on every write, exponential backoff on 5xx reusing the same key, circuit breaker. **This is the only file in the repo that imports `razorpay`.**

`scripts/live_demo.py` — end to end against a real test account: create plan → create subscription → print the auth-payment HTML → wait for `subscription.activated` → prompt the operator to trigger "Charge as Failure" from the Dashboard → consume the real `subscription.pending` webhook → decide → create a real test-mode Payment Link → mark recovered on `payment_link.paid`.

Document the test-mode limits in the script header: max 30 payment links per business, card tokens valid 3 days, test charges triggerable only from the Dashboard.

`src/vasool/chaos.py` + `make chaos` — injects, on a timer, with visible dashboard feedback: LLM 500s, Razorpay 5xx, corrupt model artefact, duplicate webhook delivery, poison queue message, issuer returning from downtime with a backlog.

**Accept**

`make live` completes a real recovery loop against a Razorpay test account. `make chaos` runs the full benchmark under continuous fault injection and still completes, with every degraded mode visible on the dashboard and zero compliance violations.

---

## Phase 10 — Submission polish

**Build**

`README.md`, in this order:
1. One sentence on what it does
2. Architecture diagram (Mermaid, rendered inline on GitHub)
3. The headline benchmark number, generated by `make bench`, with the baseline named
4. `docker compose up` → working demo in 60 seconds
5. **"Where we deliberately did not use an LLM"**
6. **"What this is not"** — the honesty section from `BUILD_DOC.md` §13, verbatim
7. Repo map, how to run the benchmark, how to run the live loop

`ARCHITECTURE.md` — the flow, the `Executor` Protocol trick, the compliance guard, the audit trail.

Final pass:
- Clone the repo into a fresh directory and run `docker compose up`. Fix whatever breaks.
- Every number in the README traced to `benchmarks/results.json`.
- `.env.example` complete; no secret in any commit, including history.
- Repo public, video link verified in a private browser window.

**Accept**

A stranger clones the repo, runs one command, and sees the number within 60 seconds.

---

## Order of operations if time runs short

Cut from the bottom, in this order: Phase 9 live loop (keep chaos), Phase 7 policy compiler and narrative (keep classifier fallback and message validation), Phase 6 Thompson sampling (keep the hazard model), Phase 6 hazard model entirely (ship `heuristic` as the headline policy).

A submission with Phases 0–5 plus a clean README, an honest benchmark, and a good video beats a submission with all ten phases half-finished. Phases 2, 4 and 5 are the product. Protect them.
