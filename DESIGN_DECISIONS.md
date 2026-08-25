# Design decisions

The what is in the code and in [ARCHITECTURE.md](ARCHITECTURE.md). This is the why — five
calls where there was a real alternative on the table and I picked the other one for a reason.

## SQLite, not Postgres

The audit log and the demo policy store are append-only and single-writer: one process, one
dashboard, one `make chaos` run at a time. Postgres would buy me connection pooling and
concurrent-writer correctness, neither of which this system needs, and I'd pay for it with a
second container in `docker-compose.yml` plus a slower cold start on top of the 77s
`docker compose up` already spends just installing LightGBM and pandas. Because SQLModel's
engine abstraction only cares about the connection string, swapping to Postgres later (if a
real merchant deployment ever needed concurrent writers) is a one-line change — nothing in
`audit/log.py` assumes SQLite specifically. I should be honest that this wasn't free:
[Engineering log #1](ENGINEERING_LOG.md) is a naive-datetime bug that SQLite's round-trip
caused directly, and that's the bill for this decision.

## `Executor` as a `Protocol`, not a base class

`RazorpayClient` and `SimulatorClient` share an interface, not a lineage. One calls a real API
over HTTPS; the other runs a seeded probability model in-process. There's nothing between them
worth inheriting. A `Protocol` (`execute/protocol.py`) makes that explicit — structural typing
means the policy layer's function signatures ask for "something with these methods," never
"something descended from this class," so nothing in `policy/` can import or special-case
either implementation, not even by accident. Invariant 5 depends on exactly that property, and
honestly so does the credibility of the whole benchmark. A base class would have made it too
easy for an `isinstance(executor, RazorpayClient)` check to creep in somewhere and quietly
break it.

## Thompson sampling, not epsilon-greedy

The exploration problem is real: for slots where the hazard model has seen little evidence,
something has to decide when to trust its point estimate and when to go find out
([`policy/explore.py`](src/vasool/policy/explore.py)). Epsilon-greedy explores a fixed random
fraction of the time no matter how much is already known about a given cell, so it keeps
re-trying the same badly-understood `(failure_class, time_bucket)` combination long after the
evidence has piled up somewhere else — and it needs a hand-tuned epsilon that has no idea how
much data actually exists per cell. Thompson sampling draws from a Beta posterior per cell
instead, so exploration scales with actual uncertainty: a cell with ten observations gets
explored far less than one with zero, with no schedule to tune by hand. It's not free either —
it needs a defined uncertainty gate (`is_uncertain()`) to decide when the posterior draw wins
over the point estimate, one more moving part than epsilon-greedy's single scalar. Either way,
invariant 2 holds: the posterior only ever updates on observed outcomes, never on anything an
LLM said.

## Groq first, Gemini as the fallback — not the other way around

Both are free-tier LLM providers behind the same `LLMClient` interface
([Engineering log #3, #4](ENGINEERING_LOG.md)). Groq goes first because its published
per-model rate limits are meaningfully more generous for this workload and it answers faster,
and both of those matter when `make chaos` and the dashboard's fault-injection panel are meant
to be watched live, not just measured afterward from a log. Gemini's free tier for the model
actually available to new API keys turned out to be 20 requests a day — I only found that by
hitting the limit during testing, not from anything published — which is too tight to be the
primary path for something meant to be clicked through repeatedly during review. I kept Gemini
as the fallback instead of dropping it, because one provider is one point of failure, which is
exactly what "never let a failing external dependency take the system down" exists to prevent.
The full reasoning is in [`llm/client.py`](src/vasool/llm/client.py)'s module docstring;
`/demo/chaos/llm` is the live proof that the fallback actually fires instead of just sitting in
a docstring.

## No Celery, no Redis

Every "background" operation in this system — the demo chaos toggles, the policy compiler,
seeding, benchmarking — either runs synchronously inside a request or is a `make` target that
runs to completion and exits. There's no durable task queue's worth of async work anywhere:
nothing here needs to survive a process restart mid-flight, get retried by a separate worker
pool, and have its results collected later. `APScheduler`, already on the approved dependency
list, covers the one genuinely time-based thing (retry-slot scheduling) in-process. Adding
Celery and Redis to a system that never actually queues cross-process work would be
infrastructure with no real job to do — exactly what CLAUDE.md's dependency rule and its
"prefer boring" standard exist to rule out.
