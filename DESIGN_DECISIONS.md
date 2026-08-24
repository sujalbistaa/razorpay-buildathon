# Design decisions

Why, not what — the what is in the code and in [ARCHITECTURE.md](ARCHITECTURE.md). Five
choices here that had a real alternative on the table, and the reason the alternative lost.

## SQLite, not Postgres

The audit log and the demo policy store are append-only and single-writer — one process, one
dashboard, one `make chaos` run. Postgres buys connection pooling and concurrent-writer
correctness this system doesn't need, at the cost of a second container in
`docker-compose.yml` and a slower cold start on top of the 77s `docker compose up` already
takes to install LightGBM and pandas. SQLModel's engine abstraction means swapping the
connection string is the only change if a real merchant deployment ever needed concurrent
writers; nothing in `audit/log.py` assumes SQLite specifically. It's worth naming that this
choice isn't free — [Engineering log #1](ENGINEERING_LOG.md) is SQLite's naive-datetime
round-trip, paid for directly by this decision.

## `Executor` as a `Protocol`, not a base class

`RazorpayClient` and `SimulatorClient` share an interface, not a lineage — one calls a real
API over HTTPS, the other runs a seeded probability model in-process, and they have nothing
in common worth inheriting. A `Protocol` (`execute/protocol.py`) makes that explicit:
structural typing means the policy layer's function signatures ask for "something with these
methods," not "something descended from this class," so nothing in `policy/` can import or
special-case either implementation even by accident. That's the property invariant 5 depends
on and the thing the whole benchmark's honesty rests on — a base class would have let a
`isinstance(executor, RazorpayClient)` check creep in somewhere and quietly break it.

## Thompson sampling, not epsilon-greedy

The system needs an exploration strategy for slots where the hazard model has seen little
evidence — [`policy/explore.py`](src/vasool/policy/explore.py). Epsilon-greedy explores
uniformly at random a fixed fraction of the time, regardless of how much is already known
about a given cell; that means it keeps randomly trying the same badly-understood
`(failure_class, time_bucket)` combinations long after evidence has piled up elsewhere, and
it needs a hand-tuned epsilon that doesn't adapt to how much data actually exists per cell.
Thompson sampling draws from a Beta posterior per cell instead, so exploration is proportional
to actual uncertainty — a cell with ten observations gets explored far less than one with
zero, without any schedule to tune. The cost is real: it requires a defined uncertainty gate
(`is_uncertain()`) deciding when to trust the hazard model's point estimate over a posterior
draw, which is one more thing to get right than epsilon-greedy's single scalar. Invariant 2
still holds regardless of which was chosen — the posterior updates on observed outcomes only,
never on anything an LLM produced.

## Groq first, Gemini as fallback — not the reverse

Both are free-tier LLM providers behind the same `LLMClient` interface
([Engineering log #3, #4](ENGINEERING_LOG.md)). The order is Groq-primary because Groq's
published per-model rate limits are meaningfully more generous for this workload, and it's
faster per call — both matter for a live demo where `make chaos` and the dashboard's
fault-injection panel are meant to be watched, not just measured after the fact. Gemini's
free tier for the model actually available to new keys turned out to be 20 requests/day,
found only by hitting the limit directly during testing, not from any published number —
too tight to be the primary path for a system meant to be clicked through repeatedly during
review. Gemini stays as the fallback rather than being dropped, because a single provider is
a single point of failure the whole "never let a failing external dependency take the system
down" invariant exists to avoid — see [`llm/client.py`](src/vasool/llm/client.py)'s module
docstring for the full reasoning, and `/demo/chaos/llm` for the live proof that the fallback
actually engages rather than just existing in a docstring.

## No Celery, no Redis

Every "background" operation here — the demo chaos toggles, the policy compiler, seeding,
benchmarking — is either synchronous within a request or a `make` target that runs to
completion and exits. There's no durable task queue's worth of async work anywhere in this
system: nothing needs to survive a process restart mid-flight, retried by a separate worker
pool, with results collected later. `APScheduler`, already in the approved dependency list,
covers the one thing that's genuinely time-based (retry-slot scheduling) in-process. Adding
Celery and Redis for a system that never actually queues cross-process work would be
infrastructure serving no real requirement — exactly what CLAUDE.md's dependency
prohibition and "prefer boring" standard rule out.
