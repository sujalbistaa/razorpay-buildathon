# CLAUDE.md

Companion documents, read both before writing any code:
- `build-docs/BUILD_DOC.md` — design and domain research. The why.
- `build-docs/BUILD_PLAN.md` — sequenced work order. The what and when.

Read this fully at the start of every session. It is the constitution of this repo. When a request in chat conflicts with a rule here, say so and ask before proceeding.

---

## What we are building

**Vasool** — an autonomous recovery agent for failed recurring payments in India. It ingests failed payment and subscription events, classifies the failure, plans a bounded sequence of retry and contact attempts subject to RBI/NPCI compliance constraints, executes them against Razorpay test-mode APIs or a simulator, stops on explicit rules, and reports rupees recovered against a named baseline.

This is a submission for the Razorpay AI Buildathon 2026, Track 03. It is graded on: **problem taste, build quality, AI judgment, failure recovery.** A reviewer will clone the repo, run one command, and look at the benchmark. Optimise for that reviewer.

The full design lives in `BUILD_DOC.md`. The sequenced work order lives in `BUILD_PLAN.md`. Do not restate them here; read them.

---

## The nine invariants

Breaking any of these is a bug, even if tests pass.

1. **Money is `int` paise. Never float, never `Decimal` at the boundary, never a bare int without the `Money` type.** All arithmetic goes through `domain.money`. Rendering to `₹1,234.56` happens only in the presentation layer.

2. **The LLM never decides timing, probability, retry eligibility, or any money arithmetic.** It classifies unmapped error strings, writes customer-facing prose, drafts policy rules for human approval, and explains structured decisions in English. That is the complete list. If a task seems to need the LLM outside that list, stop and ask. This does not prohibit the *system* from using probability — Thompson sampling in `policy/explore.py` draws from Beta posteriors updated on observed outcomes only. No LLM output ever enters a posterior, an arm choice, or a reward signal. The rule is about who computes the probability, not whether one exists.

3. **Every money action and every outbound customer message passes through `ComplianceGuard` before execution.** The guard returns `Approved` or `Rejected(rule_id, reason)`. It never silently modifies a plan. There is no bypass path, no `force=True`, no admin override.

4. **Every compliance constant carries a source comment with a URL or a regulation reference.** A magic number without a source is not allowed to exist in `compliance/`.

5. **`Executor` is a Protocol with two implementations — `RazorpayClient` and `SimulatorClient` — and the policy layer cannot tell them apart.** This is what makes the benchmark honest. Never import `razorpay` outside `execute/razorpay_client.py`.

6. **Every write to an external system carries an idempotency key derived deterministically from `(invoice_id, attempt_index, action_type)`.** Retries reuse the same key. Never generate a fresh key on retry.

7. **Every decision writes one append-only row to the audit log before the action executes**, containing the input snapshot hash, the policy version, every compliance rule evaluated with its outcome, the chosen action, the expected value, and (later, by update to a separate outcome table) the actual result. Audit rows are never updated or deleted.

8. **Simulation is seeded and reproducible.** Same seed, same `world.yaml`, same results, byte for byte. No unseeded `random`, no `datetime.now()` inside simulation or policy code — time is injected via a `Clock` Protocol.

9. **Store UTC, evaluate business rules in `Asia/Kolkata`.** Payday is a day-of-month in IST. The 24-hour pre-debit notice is measured in real hours, not calendar days. Every timestamp field is timezone-aware; a naive datetime anywhere is a bug.

---

## Hard prohibitions

- **Never write a number into README, the benchmark report, or any doc that did not come out of `make bench`.** No illustrative figures, no placeholder percentages that might survive to submission. If a number isn't computed yet, write `TBD` in capitals.
- **Never cite an industry statistic as our measured result.** External figures live in `ASSUMPTIONS.md` with a URL and are always labelled as external.
- **Never add a dependency without asking.** Current stack: FastAPI, Pydantic v2, SQLModel, APScheduler, LightGBM, numpy, pandas, structlog, httpx, anthropic, Jinja2, pytest, matplotlib. Nothing else. Specifically: no Celery, no Redis, no Kafka, no ORM other than SQLModel, no frontend build step.
- **Never use `localStorage` / `sessionStorage`.** Dashboard state is server-rendered.
- **Never let a failing external dependency take the system down.** LLM unavailable → templates and rules. Model file missing → `HeuristicPolicy`. Razorpay 5xx → queue and back off. Every fallback sets a `degraded` flag that surfaces on the dashboard.
- **Never silently swallow an exception.** Log structured, set the degraded flag, take the documented fallback path, and surface it.
- **Never mutate an audit row.**
- **Never commit secrets.** `.env` is gitignored; `.env.example` lists every key with an empty value.

---

## Code standards

- Type hints everywhere. `mypy --strict` on `domain/`, `policy/`, `compliance/`. Other packages may be looser.
- Pydantic v2 models for anything crossing a boundary (HTTP, queue, file, LLM output). LLM responses are parsed into a Pydantic model with a strict enum; a parse failure is a fallback trigger, never an exception that escapes.
- Pure functions in `policy/` and `compliance/`. No I/O, no clock reads, no DB access. Everything they need is passed in. This is what makes them testable and what makes the benchmark fast.
- `structlog` JSON logs. Every log line carries `invoice_id` and `attempt_index` when in scope.
- Docstrings on public functions state *why*, not *what*. The what is in the signature.
- Prefer boring. A dict lookup that a reviewer reads in three seconds beats a clever abstraction.

## Testing

- Policy and compliance logic is **table-driven**: a list of `(input, expected_decision, expected_rule_ids)` cases. Add a row before you add a branch.
- `tests/test_compliance_invariants.py` runs the full benchmark and asserts **zero compliance violations across every generated attempt**. This test is the product. It must never be marked xfail, skipped, or loosened. If it fails, the policy is wrong, not the test.
- `tests/test_determinism.py` asserts identical results across two runs with the same seed.
- `tests/test_idempotency.py` asserts a duplicated webhook (same `x-razorpay-event-id`) causes exactly one state transition.
- LLM calls are mocked in all tests. There is one opt-in integration test behind `RUN_LLM_TESTS=1`.
- Target: fast. `pytest` under 30 seconds, or the benchmark moves behind a marker.

## Git

- Small commits, imperative present tense, one concern each. `feat(policy): infer payday posterior from successful debit dates`.
- Never `--amend` or force-push a pushed commit. The commit history is part of the submission and it should read like two weeks of real work, because it is.
- Never commit generated artefacts except `benchmarks/results.json` and the report PNGs, which are committed deliberately so a reviewer sees the numbers without running anything.

---

## Working style with me

- **Ask before scope.** If a request is ambiguous or would take more than about 200 lines, propose the plan first in three bullets and wait.
- **Read before you write.** `ls` the package, read the adjacent module, match the existing patterns. Do not introduce a second way of doing something that already has a way.
- **Run the tests after every change.** Report the actual output, not a summary of what you expect.
- **When something is wrong with the design, say so.** If a requested feature would break an invariant, or the simpler version is obviously better, push back with the reason. Do not implement something you think is wrong and mention it afterwards.
- **When you are uncertain about a Razorpay API detail, say you are uncertain.** Do not invent field names, endpoints, error strings, or webhook event names. The real ones are in `ASSUMPTIONS.md` with source URLs; anything not there needs to be looked up before it is used.
- **Never fabricate a benchmark result, a source, or a citation.** If you need a number, run the code.

## Definition of done for any task

1. Tests pass, including `test_compliance_invariants.py`.
2. `mypy --strict` clean on the strict packages.
3. Any new constant has a source comment.
4. Any new external call has an idempotency key, a timeout, and a fallback.
5. Any new decision path writes an audit row.
6. `make bench` still runs end to end.
7. `README.md` still accurately describes what the code does.
