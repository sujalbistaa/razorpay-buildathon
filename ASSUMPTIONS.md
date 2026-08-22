# ASSUMPTIONS.md

The human-readable version of [`src/vasool/sim/world.yaml`](src/vasool/sim/world.yaml) — every
number the simulator runs on, split into what's **Sourced** (a specific published claim, with a
URL) and what's **Estimated** (reasoned, because no public source exists for it). A third
category, **Policy parameter**, isn't a claim about the world at all — it's a declared dial.

Never blur these three. The benchmark's honesty depends on it.

## Sourced

| Parameter | Value | Source |
|---|---|---|
| Payday clustering (3rd–7th, avoid 25th–31st) | mixture: 60% near 1st, 25% last working day, 15% near 7th | [razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026/](https://razorpay.com/blog/e-nach-upi-autopay-for-nbfcs-the-complete-collections-playbook-for-2026/) |
| Rail split (UPI Autopay ≤ ₹15,000, e-NACH high-value) | 65% UPI Autopay, 30% e-NACH, 5% card | same |
| AFA-free ceiling anchor | ₹15,000 (standard), ₹1,00,000 (elevated categories) | RBI Digital Payments – E-mandate Framework, 2026 (Circular RBI/DPSS/2026-27/396, 21 Apr 2026) |
| Downtime event schema (`method`, `instrument.issuer`, `severity`, `scheduled`, `begin`, `end`) | — | [razorpay.com/docs/api/payments/downtime/entity](https://razorpay.com/docs/api/payments/downtime/entity) |
| A month-end congestion effect on downtime exists | — | BUILD_DOC.md §3.2: "that's when Indian recurring debit volume spikes" |

The *shape* of each of these is sourced; most of the *magnitude* (the spread around the 1st, the
exact rail percentages, the congestion multiplier) is not published anywhere and is marked
Estimated below even where the underlying pattern is Sourced.

## Estimated

No public source publishes a per-customer distribution for any of these. Each is a reasoned
placeholder, chosen so the simulator produces a plausible failure mix — not a claim about the
real Indian payments market.

| Parameter | Value | Reasoning |
|---|---|---|
| `salary_inr` | lognormal(mean_log=10.6, sigma_log=0.5), median ≈ ₹40,100 | plausible subscription-paying salary band |
| `spend_rate` | beta(2.0, 5.0) | fraction of salary burned per day; skewed so most customers spend down steadily |
| `buffer_inr` | exponential(scale=800) | balance floor at the monthly trough; most customers run close to empty |
| `card.state_probabilities` | valid 90%, expired 3%, blocked 2%, reissued 5% | no published card-state distribution for this population |
| `mandate.state_probabilities` | active 94%, paused 3%, revoked 3% | no published mandate-health distribution |
| `mandate.max_amount_inr` | lognormal(mean_log=9.6, sigma_log=0.6) | anchored near the ₹15,000 AFA-free ceiling (Sourced), magnitude Estimated |
| `issuer_pool` | HDFC, ICICI, SBI, Axis, Kotak | illustrative, not a market-share table |
| `language_probabilities` | en 30%, hi 30%, hinglish 40% | no published language-mix source |
| `engagement.base_response_rate` | beta(2.0, 3.0) | no published response-rate data |
| `engagement.fatigue_decay` | 0.7 (multiplicative per message) | no published fatigue curve |
| `intent_to_pay_rate` | 92% | 8% genuinely churned, never recoverable regardless of strategy — a plausibility choice, not a measurement |
| `downtime_arrivals_per_day` | 0.15 per issuer (Poisson rate) | no published downtime frequency |
| `duration_hours` | lognormal(mean_log=1.0, sigma_log=0.8), median ≈ 2.7h | no published outage-duration distribution |
| `month_end_congestion_multiplier` | 2.5× | the effect is Sourced (BUILD_DOC.md §3.2); this magnitude is not |
| `severity_probabilities` | low 50%, medium 35%, high 15% | no published severity distribution |
| `issuer_base_approval_rate` | 85% | baseline approval odds absent any blocking condition |
| `bank_side_limits.velocity_exceeded_rate` | 2% per attempt | no published frequency for hitting a customer-bank velocity limit (Z7) |
| `invoice.amount_inr` | lognormal(mean_log=6.5, sigma_log=0.6), median ≈ ₹665 | illustrative D2C/SaaS/insurance-style recurring billing range |
| `invoice.category_probabilities` | standard 80%, insurance 8%, mutual_fund 7%, credit_card_bill 5% | no published category mix |
| `notification_cost_paise` | ₹0.20 | Indian transactional SMS runs roughly ₹0.12–0.25, WhatsApp utility messages a similar band (BUILD_DOC.md §4.3) — no vendor price list cited |

## Policy parameter (not a claim about the world)

| Parameter | Value | Why it isn't Estimated |
|---|---|---|
| `annoyance_cost_paise` | ₹5.00 | encodes the merchant's tolerance for contacting a customer, per attempt. There is no empirical basis to estimate — it's a declared dial. Swept across values in the Phase 8 robustness run rather than presented as a measurement. |

## Two modeling choices, flagged rather than silently made

BUILD_DOC.md §3.1 lists card states as `valid | expiring_on | blocked | reissued`, but §3.3's
resolution order checks `card.state == expired` — a name that doesn't appear in §3.1's own list.
`world.yaml` and `sim/world.py` use `expired` (matching §3.3's actual logic, since that's what's
operative) rather than `expiring_on`.

§3.3's resolution order doesn't have a branch for `intent_to_pay: false` (a customer who has
genuinely churned). Rather than adding a resolution branch the source doc doesn't list, a churned
customer's `balance(t)` is modeled as always `₹0` — so "never recoverable" falls out of the
existing `balance(t) < amount → INSUFFICIENT_FUNDS` branch causally, instead of needing a special
case. See `BalanceProcess.balance_at` in `sim/world.py`.

## What this means for interpreting the benchmark

Every number `make bench` prints is measured against *this* simulator, not against the real
Indian payments market. The Sourced parameters anchor the simulator's shape to real, published
constraints (the AFA ceiling, the payday-clustering pattern, the downtime event schema); the
Estimated parameters and the one Policy parameter are where the simulator's absolute numbers
could be wrong even if its qualitative behavior (soft declines recovering, hard declines not,
downtime-gated retries reducing false dunning) is right. The robustness sweep (BUILD_DOC.md §8)
exists specifically to show which part of the result survives when the Estimated parameters are
perturbed ±30–50% — that sweep, not the headline number, is the credible claim.
