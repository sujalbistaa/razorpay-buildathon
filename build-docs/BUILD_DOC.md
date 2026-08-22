# Vasool — Build Document

**Submission target:** Razorpay AI Buildathon 2026, Track 03 — AI Revenue Recovery
**Deadline:** 5 September 2026 (rolling shortlist through September)
**One-line:** An autonomous recovery agent for failed recurring payments in India that decides *whether, when, how and through which rail* to retry each failed debit, executes a bounded and RBI-compliant recovery workflow, and reports rupees recovered against Razorpay's own documented retry baseline.

*(Name is Hindi for "recovery/collection". Rename freely — `git grep -l vasool | xargs sed -i` and move on.)*

---

## 0. Why this wins, stated bluntly

Razorpay's published bar for this track:

> "Don't just identify the problem. Show measured money recovered across a batch, with compliant escalation, stopping rules, and an audit trail."
> — https://razorpay.com/buildathon/

Their scoring dimensions: **Problem Taste, Build Quality, AI Judgment, Failure Recovery.**

Most submissions in this track will be an LLM that reads a failed payment and writes a nice email. They will have no baseline, no number, no compliance layer, and no audit trail. They will lose on all four dimensions.

This build maps one-to-one onto the bar:

| Their words | What we ship |
|---|---|
| measured money recovered | Head-to-head benchmark vs 4 baselines, paired seeds, bootstrap CI, `make bench` reproduces it |
| across a batch | 2,000 failed invoices / 500 customers / 90-day horizon |
| compliant escalation | `ComplianceGuard` — hard gate on every money action, sourced to RBI/NPCI/Razorpay docs, 0 violations asserted in tests |
| stopping rules | Explicit `StopRule` enum, EV-based termination, hard-decline suppression, promise-to-pay suppression |
| audit trail | Append-only decision log; every attempt replays its full input snapshot, rule evaluations and expected value |
| AI Judgment | A README section titled *"Where we deliberately did not use an LLM"* |
| Failure Recovery | `chaos` mode that kills the LLM, the Razorpay API and the model file live on camera; system degrades and keeps collecting |

---

## 1. Domain research (the part that makes this credible)

Everything below is sourced. Put the same sources in `ASSUMPTIONS.md` in the repo.

### 1.1 The baseline we are beating is published

Razorpay Subscriptions retries a failed auto-charge **once a day for three days after the charge date (T+1, T+2, T+3)**, then moves the subscription to `halted`. In test mode, four consecutive failures exhaust retries.
→ https://razorpay.com/docs/payments/subscriptions/payment-retries/
→ https://razorpay.com/docs/payments/subscriptions/test/

This is a gift. It means the benchmark baseline is not a strawman we invented; it is the incumbent behaviour of the platform we are pitching to. Name it `razorpay_default` in code and cite the URL in a comment.

### 1.2 The failure taxonomy is published, and it is causal

Razorpay exposes a structured error object: `code`, `description`, `source` (customer / business / razorpay / gateway / bank / network), `step` (payment_authentication / payment_authorization / ...), `reason`.
→ https://razorpay.com/docs/errors/

Documented card reasons include: `payment_timed_out`, `gateway_technical_error`, `payment_cancelled`, `card_declined`, `insufficient_funds`, `card_not_enrolled`, `bank_technical_error`, `card_disabled_for_online_payments`, `authentication_failed`, `payment_risk_check_failed`, `payment_failed`, `incorrect_cvv`, `debit_instrument_inactive`, `debit_instrument_blocked`, `card_expired`, `transaction_limit_exceeded`.
→ https://razorpay.com/docs/errors/payments/cards/

UPI/NPCI codes, per Razorpay's own blog: `Z9` insufficient funds, `U28` customer bank down, `U30` debit failed, `U69` collect request expired, `Z7` velocity limit at customer bank, `Z8` per-transaction limit at customer bank.
→ https://razorpay.com/blog/tackling-upi-payment-failures-with-razorpay/

**The insight the whole product rests on:** these reasons have completely different recovery-probability curves over time.
- `Z9` / `insufficient_funds` → recovers when money lands in the account. Payday-shaped.
- `U28` / `gateway_technical_error` / `bank_technical_error` → recovers in minutes to hours. Downtime-shaped.
- `Z7` / `transaction_limit_exceeded` → recovers when the velocity window resets. Calendar-shaped.
- `card_expired` / `debit_instrument_blocked` / mandate revoked → never recovers silently. Needs a credential change, i.e. a human.
- `payment_risk_check_failed` → must not be retried at all.

A fixed T+1/T+2/T+3 schedule is optimal for none of these, and actively harmful for the last two.

### 1.3 India-specific constraints that nobody else will model

**RBI Digital Payments – E-mandate Framework, 2026** (Circular RBI/DPSS/2026-27/396, 21 April 2026):
- Pre-transaction notification to the customer **at least 24 hours before every recurring debit**, carrying merchant name, amount, debit date/time, mandate reference, reason, and an opt-out.
- AFA-free ceiling **₹15,000** per recurring transaction; **₹1,00,000** for insurance premiums, mutual funds and credit-card bills registered under e-mandate. Above the applicable threshold, AFA every time.
- FASTag / NCMC auto-replenishment exempt from pre-debit notification.

This is a *hard scheduling constraint*, and it is the single most India-specific thing in the build. You cannot "retry in 20 minutes" on a mandate rail. Your retry planner has to reason about a 24-hour notification lead time, which means every plan is really a plan over `notify_at` and `debit_at` pairs. No US-copied retry design handles this. Model it and say so.

**Razorpay's own published payday heuristic:**
> "Avoid debit dates on the 25th to 31st; align to the 3rd to 7th alongside the 24-hour pre-debit notification to raise first-attempt success."
> — https://razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026/

Use this as the *validation target*, not the mechanism. Our system infers each customer's payday individually rather than applying a blanket 3rd-to-7th rule, and we show that the population-level output of our per-customer inference reproduces Razorpay's published guidance. That is a very strong slide: our model rediscovers their heuristic from data, then beats it by personalising.

**Rail selection:** UPI Autopay for ≤ ₹15,000 (AFA-free, mobile-first), e-NACH for high-value / long-tenure. Non-revocable UPI Autopay mandates exist for regulated-lender EMI collection. Same source as above.

### 1.4 Retry patterns and the network rules that bound them

From the payment-orchestration literature (Gr4vy, 2 June 2026 — https://gr4vy.com/posts/payment-retry-logic-explained-smart-retries-for-failed-transactions-in-2026/):
- 70–90% of failed card-not-present payments are *soft* declines and are recoverable.
- Subscription businesses lose roughly 9% of revenue to failed payments; up to 70% of involuntary churn traces to failed transactions.
- Visa caps merchant-initiated retries at 15 attempts / 120 days for the same transaction; certain codes prohibit retry entirely. Mastercard runs an Excessive Decline Rate programme.
- Practical envelope: 3–5 attempts over 10–14 days, at least 24h between attempts on the same path.
- The worst possible pattern is repeated same-path retries within minutes — it fails *and* looks fraudulent to the issuer.

Cite these as industry figures, not as your measured results. Never blur the two.

### 1.5 The live-data hook most people will miss

Razorpay exposes a **Payment Downtime API and webhooks** — `payment.downtime.started / updated / resolved` — with `method`, `instrument.issuer`, `instrument.card_type`, `severity` (low/medium/high), `scheduled` (bool), `begin`, `end`.
→ https://razorpay.com/docs/api/payments/downtime/entity

Gate retries on live issuer downtime. If HDFC credit is `severity: high` and unresolved, do not burn an attempt and *do not* send the customer a "your payment failed, please update your card" message — it isn't their card, it's the bank. That single behaviour is a product-taste signal a Razorpay engineer will notice immediately, and it produces a metric no other submission will report (§5.3, *false dunning rate*).

### 1.6 What is actually reachable in test mode

Verified against docs, so the live demo can't blow up:
- Create Plan → Create Subscription → authenticate via Checkout with `subscription_id`. Test card, then `Success`.
- **Dashboard "Charge this now" in test mode lets you choose Success or Failure.** Choosing Failure moves the subscription `active → pending`, fires `subscription.pending`, increments the attempt counter, and pushes `next charge` by one day. Four failures → `halted` + `subscription.halted`.
- Card tokens in test mode are valid **3 days** — subsequent debits must happen inside that window.
- Payment Links: create standard or UPI (`upi_link: true`), with `customer`, `notify: {sms, email}`, `reminder_enable`, `expire_by`, `reference_id`, `notes`, `callback_url`. **Test mode allows max 30 payment links per business.**
- Webhooks: `X-Razorpay-Signature` = HMAC-SHA256 hex of the **raw** body keyed with the webhook secret; `x-razorpay-event-id` is unique per event and is your idempotency key. Razorpay retries failed deliveries with exponential backoff for 24h then disables the webhook.

**Therefore:** the *batch* is simulated (2,000 invoices), and a *small live loop* (3–5 subscriptions) runs against real Razorpay test-mode APIs. Say exactly this, in these words, in the README and the video. Precision about which half is which is what makes the whole thing believable.

---

## 2. What the system does

```
webhook / batch job
      │
      ▼
┌─────────────┐   raw error + context
│  DIAGNOSE   │──────────────────────────────► FailureClass (+confidence, +evidence)
└─────────────┘   rules table first; LLM only for unmapped strings
      │
      ▼
┌─────────────┐   customer state, issuer downtime, calendar, history
│   POLICY    │──────────────────────────────► RecoveryPlan = [Attempt, ...] + StopRule
└─────────────┘   hazard model → P(success | t, class, ctx) → EV-maximising plan
      │
      ▼
┌─────────────┐   HARD GATE. Rejects with a named rule, never silently mutates.
│ COMPLIANCE  │──────────────────────────────► ApprovedPlan | Rejection(rule, reason)
└─────────────┘   24h pre-debit, AFA limits, attempt caps, quiet hours, issuer buckets
      │
      ▼
┌─────────────┐   idempotent, circuit-broken, retried with backoff
│  EXECUTOR   │──────────────────────────────► Razorpay test-mode API │ Simulator
└─────────────┘   same interface for both — this is why the benchmark is honest
      │
      ▼
┌─────────────┐
│    AUDIT    │  append-only. input hash, policy version, every rule evaluated,
└─────────────┘  chosen action, expected value, actual outcome, latency.
```

`COMMS` sits beside the executor: it generates the customer-facing message (English / Hindi / Hinglish) only when the policy has decided a message is the right action, and every generated message passes a validator before it can be sent.

---

## 3. The simulator is the intellectual core

Do not fit a curve and sample from it. Build a **causal generative world**, hide its state from the policy, and let the error codes be the only channel of information. Then beating the baseline is a real inference problem, and the *reason* for the lift is explainable rather than tautological.

### 3.1 Latent customer state

```yaml
customer:
  payday_dom: mixture           # 60% 1st±2, 25% last working day, 15% 7th/10th
  balance:                      # jumps at payday, decays through the month
    salary_inr: lognormal
    spend_rate: beta            # fraction of salary burned per day
    buffer_inr: exponential     # what stays in the account at trough
  card:
    state: valid | expiring_on | blocked | reissued
  mandate:
    rail: upi_autopay | enach | card
    state: active | paused | revoked
    max_amount_inr: int
    revocable: bool
  issuer: str                   # each issuer has its own availability process
  engagement:
    base_response_rate: beta
    fatigue_decay: float        # every message sent lowers the next one's odds
  intent_to_pay: bool           # some customers have genuinely churned. Never recoverable.
```

### 3.2 Issuer availability process

Poisson-arrival downtime bursts, log-normal durations, with a month-end congestion multiplier (25th–31st) because that's when Indian recurring debit volume spikes. Emits synthetic `payment.downtime.*` events on the same schema as Razorpay's real ones, so the downtime-gating code path is identical in sim and live.

### 3.3 Attempt resolution — evaluated in this order

```
mandate.state != active              → MANDATE_REVOKED           (hard)
card.state == expired                → CARD_EXPIRED              (hard)
card.state == blocked                → DEBIT_INSTRUMENT_BLOCKED  (hard)
issuer down at t                     → GATEWAY_TECHNICAL_ERROR / U28
amount > mandate.max_amount          → TRANSACTION_LIMIT_EXCEEDED
amount > AFA limit and no AFA        → AUTHENTICATION_REQUIRED
velocity(window) exceeded            → Z7
balance(t) < amount                  → INSUFFICIENT_FUNDS / Z9
else                                 → success w.p. issuer_base_approval_rate
```

Every parameter lives in `sim/world.yaml` with an inline comment marking it as **sourced** (with URL) or **estimated** (with reasoning). `ASSUMPTIONS.md` reproduces the full list. This is the file a skeptical judge opens first; make it the most honest file in the repo.

---

## 4. The policy

### 4.1 `BaselinePolicy` (the things we beat)

- `no_retry` — floor.
- `razorpay_default` — T+1, T+2, T+3 daily, then halt. Cite the docs URL in the class docstring.
- `static_1_3_7` — the common merchant-configured schedule.
- `dunning_only` — message immediately, never silently retry.

### 4.2 `HeuristicPolicy` (ship this on day 5, it is your insurance)

A decision table keyed on `FailureClass`, with payday-aware scheduling. Deterministic, sub-millisecond, fully explainable, and it is what the system falls back to when the model or the LLM is unavailable. Roughly:

| Class | Action | Timing |
|---|---|---|
| `INSUFFICIENT_FUNDS` | silent retry | next inferred payday + 1 day, notify 24h before |
| `GATEWAY_TECHNICAL_ERROR` / `U28` | silent retry | on downtime `resolved`, + jitter; suppress until then |
| `VELOCITY_EXCEEDED` / `Z7` | silent retry | next day, off-peak |
| `TRANSACTION_LIMIT_EXCEEDED` | split or alternate rail | next cycle |
| `AUTHENTICATION_FAILED` | customer contact (link) | immediately, within quiet-hour window |
| `CARD_EXPIRED` / `BLOCKED` / `MANDATE_REVOKED` | credential-update request | once; then stop |
| `PAYMENT_RISK_CHECK_FAILED` | stop | never retry |
| `UNKNOWN` | treat as ambiguous soft | one retry at T+3, then contact |

### 4.3 `LearnedPolicy` (the differentiator)

**Payday inference.** Per customer, a posterior over day-of-month from their historical *successful* debit dates plus the observed `Z9` dates. Nobody tells you the payday; you infer it. Validation plot: inferred vs the simulator's hidden true `payday_dom`, and separately, the population histogram of chosen debit dates — which should land on the 3rd–7th, reproducing Razorpay's published guidance from data. **That plot is the single best slide in the video.**

**Hazard model.** Discrete-time survival. For each candidate slot `t` in the next 14 days:

```
P(success | failure_class, days_since_failure, days_relative_to_inferred_payday,
            issuer_up(t), attempt_index, amount_bucket, rail, hour_bucket)
```

Gradient-boosted logistic regression (LightGBM), trained on an exploration log generated from cohort A. Not a neural network. If a judge asks why, the answer is "a few thousand attempts, tabular features, needs to be explainable to a compliance reviewer" — and that answer is worth more than a transformer.

**Planning.** Expected value of an attempt:

```
EV(a) = P(success | a) × amount
      − notification_cost(a)               # mandatory pre-debit notice send: SMS/WhatsApp/email
      − annoyance_cost(a)                  # declared policy dial, not a measurement — see below
```

Issuer downtime is **not** a cost term. A cost lets a value-maximiser buy its way through a known outage; that's wrong on its face — an attempt against a `severity: high`, unresolved issuer is not eligible, full stop. It is a `ComplianceGuard` gate (`R015_ISSUER_DOWNTIME_GATE`, §5), same tier as a hard decline. This also means the false-dunning-rate metric (§5.3) falls out of the gate rather than being computed as a separate pass.

`notification_cost(a)` is the one attempt cost that's actually mandatory: Razorpay bills on successful transactions, so a failed retry has ~zero gateway fee, but the RBI e-mandate framework requires a pre-debit notification ≥24h ahead of every mandate debit, and that send costs something. Lives in `world.yaml` as `notification_cost_paise`, marked `ESTIMATED` (Indian transactional SMS ~₹0.12–0.25, WhatsApp utility messages similar band — no vendor price list cited as a source).

`annoyance_cost(a)` cannot be sourced and must not pretend to be. It's a declared `annoyance_cost_paise` policy dial in `world.yaml`, marked `POLICY PARAMETER, NOT ESTIMATED` — it encodes the merchant's tolerance for contacting customers and has no empirical basis. Sweep it across at least three values in the Phase 8 robustness run and report how plan shape changes. A dial labelled as a dial and swept is honest; one disguised as a measurement is not.

Greedy with lookahead depth 2 over the constrained action set. Explicitly *not* RL — write one sentence in the README saying why, because saying no to RL correctly is exactly the "AI Judgment" signal.

**Exploration.** Thompson sampling (Beta posterior per `failure_class × time-bucket` cell) applied only where the hazard model's uncertainty is high, so the system keeps learning without spending real money on bad arms. One-line citation: Agrawal & Goyal, *Thompson Sampling for Contextual Bandits with Linear Payoffs*, ICML 2013.

### 4.4 Stopping rules — enumerate them, they're explicitly in the bar

```python
class StopRule(StrEnum):
    HARD_DECLINE          # class is in HARD_DECLINE_CLASSES
    MAX_ATTEMPTS          # 4 silent attempts per invoice
    WINDOW_EXPIRED        # 14 days from first failure
    NEGATIVE_EV           # best remaining attempt has EV <= 0
    MANDATE_REVOKED
    CUSTOMER_OPTED_OUT
    PROMISE_TO_PAY        # suppress until the promised date, then one attempt
    INVOICE_PAID
```

`PROMISE_TO_PAY` is worth building: the recovery link page carries a "I'll pay on ___" control, which records a PTP, silences all attempts until that date, and fires exactly one attempt then. Razorpay's own track description lists "Promise-to-pay tracker" as an example direction. Cheap to build, directly on their list.

---

## 5. Compliance guard

Every money action and every customer message passes through it. It returns `Approved` or `Rejected(rule_id, human_reason)`. It never silently modifies a plan.

### 5.1 Constants, each with a source comment in code

```python
PRE_DEBIT_NOTICE_HOURS      = 24        # RBI E-mandate Framework 2026
AFA_FREE_LIMIT_PAISE        = 15_00_000
AFA_FREE_ELEVATED_PAISE     = 1_00_00_000   # insurance, mutual_fund, credit_card_bill
MAX_SILENT_ATTEMPTS         = 4          # matches Razorpay subscription retry exhaustion
MAX_ATTEMPT_WINDOW_DAYS     = 14
MIN_HOURS_BETWEEN_SAME_PATH = 24         # card-network guidance
CONTACT_QUIET_HOURS_IST     = (21, 9)
MAX_MESSAGES_PER_INVOICE    = 3
MIN_HOURS_BETWEEN_MESSAGES  = 48
HARD_DECLINE_CLASSES        = {CARD_EXPIRED, DEBIT_INSTRUMENT_BLOCKED,
                               MANDATE_REVOKED, PAYMENT_RISK_CHECK_FAILED}
```

Plus a **per-issuer token bucket** so that when an issuer comes back from downtime you don't slam it with 400 queued retries in one second. Retry storms are the classic failure of naive recovery systems and modelling one is a strong signal.

`R015_ISSUER_DOWNTIME_GATE` — a mandate debit is not eligible while `payment.downtime` reports the relevant `(method, issuer)` at `severity: high` and unresolved (§1.5, §3.2). No numeric constant: the source is the downtime event itself, sourced live from the Payment Downtime API in the live loop and from the simulator's `IssuerAvailability` process in the benchmark, same schema either way.

### 5.2 The assertion that goes in the pitch

`test_compliance_invariants.py` runs the full benchmark and asserts **zero violations across every attempt generated** (~10,000+). "Zero compliance violations across 10,000 simulated attempts, enforced by a test that fails the build" is one sentence in the video and it lands hard.

### 5.3 False dunning rate — the metric nobody else reports

Fraction of customer messages sent for failures whose `source` was `bank` / `gateway` / `razorpay` rather than `customer`. Telling someone to update their card when HDFC was down for 40 minutes is a support ticket you caused. The baseline incurs this; we drive it near zero via downtime gating. Report it next to the recovery rate, always.

---

## 6. Where the LLM earns its place

**Used for:**
1. **Unmapped-error classification.** Rules table first. LLM only when the reason string doesn't match. Output constrained to the `FailureClass` enum plus confidence and evidence span; below-threshold → `UNKNOWN` → ambiguous-soft policy. Never invents a class.
2. **Batch root-cause narrative.** *"41% of last Tuesday's failures were HDFC debit issuer downtime between 02:10 and 03:40 IST. Those customers were messaged by the old schedule and shouldn't have been."* Detection → diagnosis → recommended action, in the merchant's language.
3. **Recovery message generation.** English / Hindi / Hinglish, tone calibrated to failure class. `insufficient_funds` gets *"we'll try again on the 4th, or pay now"*, not *"your card was declined"*.
4. **Natural-language policy authoring.** Merchant types *"don't retry anything under ₹100 more than twice"*; the LLM compiles it to a typed `PolicyRule`, which is validated, diffed against the live policy, and shown as structured JSON for explicit confirmation before it activates. The LLM writes a proposal; a human approves a diff.
5. **Audit-trail explanation.** Turns the structured decision record into one human sentence. The structured record is authoritative; the sentence is derived and never load-bearing.

**Message validator (mandatory, before any send):**
- amount, debit date and merchant name must appear verbatim from the record — string-equality check against canonical values, not a vibe check
- opt-out line present
- length caps per channel
- no threats, no legal claims, no invented consequences, no invented dates
- fail → fall back to the deterministic template, log `llm_validation_failed`, continue

**Not used for:** retry timing, probability estimation, deciding hard-decline retry eligibility, computing recovered amounts, or anything that touches money arithmetic. Put this list in the README under the heading **"Where we deliberately did not use an LLM."** It is the highest-signal paragraph in the whole submission.

---

## 7. Failure recovery — a scored criterion, so demo it

Build these, and trigger them **live in the video** via `make chaos`:

| Injected failure | Designed response |
|---|---|
| LLM API timeout / 500 | template messages + rules-only classifier; `degraded: llm` badge on dashboard; collection continues |
| Razorpay API 5xx | exponential backoff reusing the **same idempotency key**; circuit breaker opens after N; attempts queued, never dropped |
| Webhook signature mismatch | 400, structured log, alert counter, zero state change |
| Duplicate webhook delivery | dedupe on `x-razorpay-event-id`; no double charge; test proves it |
| Model artefact missing/corrupt | fall back to `HeuristicPolicy`, loud warning, keep serving |
| Poison queue message | dead-letter table + `/admin/dlq/replay` endpoint |
| Issuer returns from downtime | token bucket drains the backlog at a safe rate instead of a thundering herd |
| Clock/timezone | store UTC, evaluate every business rule in `Asia/Kolkata`; payday is a DOM in IST |

Thirty seconds of `make chaos` on camera — LLM killed, Razorpay 500ing, system visibly degrading and still recovering money — wins this criterion outright.

---

## 8. Benchmark protocol

Make it hard to attack, and pre-empt the one attack that is actually valid.

- **Cohorts:** 500 customers, 2,000 failed invoices, 90-day horizon, fixed seeds.
- **Split by customer, not by attempt.** Learned policy trains on cohort A's exploration log, evaluates on held-out cohort B.
- **Arms:** `no_retry`, `razorpay_default`, `static_1_3_7`, `dunning_only`, `heuristic`, `learned`.
- **Paired:** identical world, identical seeds, identical latent customer states across all arms. Report the paired difference with a bootstrap 95% CI.

**Metrics table, per arm:**

| recovery rate | ₹ recovered | attempts per recovery | mean days to recovery | messages sent | false dunning rate | compliance violations |
|---|---|---|---|---|---|---|

**The valid attack:** *"your model learned the simulator you wrote, of course it wins."* Answer it before it's asked, with two things:

1. **Robustness sweep.** Re-run with world parameters perturbed ±30–50% — payday distribution shifted, downtime rate doubled, hard-decline mix tripled, engagement halved — and report the lift under each. Publish a table where the lift shrinks. If it survives misspecification, the honest claim is *"reason-aware, payday-aligned, downtime-gated retry beats fixed schedules across a wide parameter range"*, which is defensible and true.
2. **Ablation.** Contribution of each component: reason-awareness alone → + payday inference → + downtime gating → + EV-based stopping → + dunning. A stacked bar. This is the chart that goes in the video, because it shows *why* the number moved, which is worth more than the number.

`make bench` writes `benchmarks/results.json`, `benchmarks/report.md` and the PNGs. One command, no arguments, reproducible from a clean clone.

---

## 9. Stack and repo

**Python 3.11 · FastAPI · Pydantic v2 · SQLModel + SQLite (Postgres optional) · APScheduler · LightGBM · structlog · pytest · Jinja2 + HTMX + Tailwind CDN + Chart.js**

No Celery, no Redis, no Kafka. `docker compose up` starts one container and the demo works. A judge who can't run your repo in 60 seconds scores you on the README alone.

Frontend recommendation: HTMX over Next.js. Fourteen days, and the engine is what's being judged. HTMX + Tailwind CDN + Chart.js gives a dashboard that looks good on video with zero build step and zero risk of a broken `npm install` on the reviewer's machine.

```
vasool/
├── README.md                  ← architecture diagram in the first screen
├── CLAUDE.md                  ← rules for Claude Code
├── ASSUMPTIONS.md             ← every number, sourced or marked estimated
├── ARCHITECTURE.md
├── docker-compose.yml
├── Makefile                   ← up / seed / bench / chaos / test / live
├── src/vasool/
│   ├── domain/         types, enums, Money(paise:int), FailureClass, Attempt, RecoveryPlan
│   ├── diagnose/       rules table, llm fallback classifier, confidence gating
│   ├── policy/         baseline, heuristic, learned, hazard model, payday inference, planner
│   ├── compliance/     rule engine, constants (with source URLs), token buckets
│   ├── execute/        RazorpayClient | SimulatorClient (one Protocol), idempotency, breaker
│   ├── comms/          message generation, validators, channels
│   ├── audit/          append-only decision log, replay, export
│   ├── sim/            world model, world.yaml, generators
│   ├── bench/          harness, metrics, ablation, robustness sweep, plots
│   └── api/            webhooks, dashboard, admin, decision inspector
├── tests/
└── benchmarks/
```

---

## 10. Fourteen-day plan

**Get a number by day 5.** Everything after day 5 is upside; without day 5 you have nothing.

| Days | Deliverable | Done when |
|---|---|---|
| 1–2 | Domain types, failure taxonomy, compliance engine + constants | `pytest tests/test_compliance.py` green, table-driven |
| 3–4 | Simulator + `world.yaml` + `ASSUMPTIONS.md` | 2,000 invoices generate reproducibly from a seed |
| 5 | Baselines + benchmark harness | **`make bench` prints a real number for `razorpay_default`** |
| 6–7 | Heuristic policy, payday inference, downtime gating | number moves; inferred-vs-true payday plot exists |
| 8–9 | Hazard model, EV planner, ablation, robustness sweep | ablation stacked bar + perturbation table |
| 10 | LLM layer: classifier fallback, messages + validators, NL policy compiler, narrative | validator rejects a deliberately bad generation in a test |
| 11 | Dashboard + decision inspector | click a payment, see the full reasoning chain and every rule evaluated |
| 12 | Live Razorpay test-mode loop + `make chaos` | real webhook → real decision → real test-mode Payment Link |
| 13 | README, architecture diagram, record video | repo runs clean from `git clone` on a fresh machine |
| 14 | Buffer, form, submit | — |

Commit daily. Real commit history over two weeks reads as real work; one 4,000-line dump the night before reads as something else.

---

## 11. Video (5 minutes, hard cuts, no slides for the first four)

| Time | Content |
|---|---|
| 0:00–0:20 | The money. Failed recurring debits, involuntary churn, what a fixed T+1/T+2/T+3 schedule costs. One number on screen. |
| 0:20–0:50 | Why fixed retries are wrong: four error codes, four completely different recovery curves. One diagram. |
| 0:50–3:00 | **Live product.** Real terminal, real dashboard. Feed a batch. Show the decision inspector on one `Z9` — inferred payday, the 24h pre-debit constraint, the chosen slot, the EV. Show a downtime `U28` being suppressed, and the customer *not* being messaged. Show a `card_expired` stopping immediately with a credential-update link. |
| 3:00–3:30 | `make chaos`. Kill the LLM. 500 the Razorpay API. System degrades visibly and keeps collecting. |
| 3:30–4:15 | `make bench`. The head-to-head table, the ablation bar, the robustness sweep. State the honest claim in the honest words. |
| 4:15–5:00 | What breaks at scale, what you'd build next, what you'd need real data to calibrate. |

No intro music. No animated logo. Do not narrate your resume. Speak fast.

---

## 12. Form answers

**Track:** Track 3: AI Revenue Recovery

**Project Objectives — what does it solve?**
Draft (one number, one mechanism, no adjectives):

> Failed recurring debits are retried on a fixed schedule that ignores why they failed. Vasool decides per-invoice whether, when and on which rail to retry, by inferring each customer's salary cycle and gating on live issuer downtime, then executes a bounded RBI-compliant recovery workflow with explicit stopping rules and a full audit trail. Across a 2,000-invoice batch it recovers **X%** more than Razorpay's documented T+1/T+2/T+3 subscription retry schedule (₹Y vs ₹Z), with zero compliance violations and an **N%** reduction in messages sent to customers whose payment failed because of bank downtime rather than anything they did.

Fill X, Y, Z, N from `benchmarks/results.json` on submission day. Nothing else.

**GitHub Repository URL** — public, README-first, `make bench` reproduces every number in the video.

**Build Challenges & Technical Obstacles** — *do not fabricate; fill from your real log.* The shape that scores:

> concrete failure → what you measured → the trade-off you took → what you'd do differently

Likely real candidates from this build, to record as they happen:
- The 24-hour pre-debit notification turns a scheduling problem into a two-variable one (`notify_at`, `debit_at`); the first planner treated them as one and produced plans the compliance guard rejected wholesale. Fix: attempts became `(notify_at, debit_at)` pairs and the planner searches over notify slots.
- Payday inference on customers with fewer than three historical successes is unidentifiable. Fix: shrink to a population prior, and flag low-confidence customers to the heuristic path rather than pretending to know.
- Issuers returning from downtime produced a retry storm in the first version. Fix: per-issuer token bucket, measured backlog drain rate.
- LLM latency put 2–4s into the decision path. Fix: moved it off the critical path entirely; classification is rules-first with an async LLM fallback, messages are generated after the timing decision is already made.
- Test-mode limits (30 payment links, 3-day card tokens) forced the split between the simulated batch and the live loop, which turned out to be the right architecture anyway — one `Executor` Protocol, two implementations.

**Final Submission Confirmation** — irreversible. Check it last, after the repo is public and the video link is verified in a private window.

---

## 13. The honesty section (put this in the README)

> **What this is not.** The 2,000-invoice batch is synthetic. The lift is measured against a simulator whose parameters are documented in `ASSUMPTIONS.md`, sourced where public sources exist and marked as estimates where they don't. The learned policy is trained on data generated by that simulator, so the absolute number is not a forecast of real-world performance; the robustness sweep in §8 is there to show which part of the result survives parameter misspecification. On real merchant data the hazard model would need recalibration and the payday prior would be re-fit. The live loop against Razorpay test-mode APIs is 3–5 subscriptions, not the batch.

This costs a paragraph and buys the reader's trust for the other forty. Payments engineers have all seen inflated recovery claims. Being the one submission that draws the line itself is worth more than two points of simulated lift.
