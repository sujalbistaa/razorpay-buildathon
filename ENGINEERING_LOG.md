# Engineering log

A benchmark number is easy to fake. A debugged incident isn't, so I'm keeping six real bugs I
actually hit while building this — each one as symptom, root cause, fix, with the file that
proves it. Nothing here is illustrative. Every one of these happened, for real, while I was
working.

## 1. A naive datetime slipped past `AwareDatetime` after a SQLite round-trip

**Symptom.** `/demo/explain` — the "explain this decision in English" button on the audit row
— threw a 500: `pydantic_core.ValidationError: decided_at Input should have timezone info`.
The row it was reading had literally just been written by the same process, with a real
timezone-aware `datetime` on it.

**Root cause.** SQLite has no native timestamp type. SQLModel stores a `datetime` as text and
reads it back **naive**, no matter what was actually written. `Decision.decided_at` is typed
`AwareDatetime` (invariant 9 says outright: "a naive datetime anywhere is a bug"), so the
object built straight from the row failed validation the instant it got reconstructed for
anything beyond raw column access.

**Fix.** [`src/vasool/api/dashboard.py`](src/vasool/api/dashboard.py)'s
`_reconstruct_decision` reattaches UTC explicitly on the way out of SQLite. Invariant 9 already
guarantees that anything stored is UTC, so this isn't a guess on my part:

```python
# SQLite round-trips a datetime as naive regardless of what was written -- invariant 9
# ("store UTC") means this is always UTC on the way back out, so we reattach it here
# rather than let a naive datetime reach Decision's AwareDatetime field.
decided_at = row.decided_at.replace(tzinfo=UTC)
```

## 2. Gemini's own docs describe a request shape that 400s on the live API

**Symptom.** Structured-output calls to Gemini failed immediately with `Invalid value at
'generation_config.response_format.text.mime_type'` — a hard 400, not the kind of transient
error a fallback is supposed to catch.

**Root cause.** The documented shape for constrained JSON output is
`generationConfig.responseFormat.text.{mimeType,schema}`. That's exactly what the reference
docs show. The live `v1beta` endpoint rejects it outright anyway.

**Fix.** I found the shape it actually accepts by testing directly against the real endpoint
instead of re-reading the docs harder — a flat `generationConfig.responseMimeType` /
`.responseSchema`, no nested `text` object at all. See
[`src/vasool/llm/client.py:55-56`](src/vasool/llm/client.py):

```python
# docs either: generationConfig.responseFormat.text.{mimeType,schema} 400s, the flat
# generationConfig.responseMimeType / .responseSchema is what the live API actually wants.
```

## 3. `gemini-2.5-flash`, the model every guide names, 404s for new keys

**Symptom.** Every single call using the widely-documented `gemini-2.5-flash` model name came
back with a hard 404.

**Root cause.** Google quietly stopped issuing `gemini-2.5-flash` access to API keys created
after some cutoff date — "no longer available to new users," and that line only shows up in
the 404 body itself, not on any docs page I could find. A key you create today gets handed a
model name every published guide still confidently recommends, and it just doesn't work.

**Fix.** Google's own error names the replacement, so I switched to `gemini-3.6-flash` and
confirmed it with a real successful call before adopting it — see
[`src/vasool/llm/client.py:51-57`](src/vasool/llm/client.py).

## 4. Two Groq model names in every guide are dead

**Symptom.** `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` — the two models nearly
every Groq quickstart still points at — both failed outright.

**Root cause.** Both were deprecated and pulled on 16 Aug 2026. Guides and cached blog posts
haven't caught up.

**Fix.** Instead of trusting another guide, I queried the key's own available models directly
(`GET /openai/v1/models`), found `openai/gpt-oss-20b`, and confirmed it end-to-end — plain
chat and `json_schema` structured output both — against the live API before adopting it. See
[`src/vasool/llm/client.py:43-47`](src/vasool/llm/client.py).

## 5. `MAX_TOKENS` was silently indistinguishable from a genuine refusal

**Symptom.** Calls made with a deliberately tight token budget — a caller asking for a short
reply — came back empty, and the code was treating that exactly like the model refusing to
answer.

**Root cause.** Both Groq's and Gemini's models burn hidden "thinking" tokens before producing
any visible output, and those thinking tokens get billed against the *same* output-token
budget the caller asked for. A budget of 200 tokens burned roughly 190 of them on invisible
Gemini reasoning alone and hit `MAX_TOKENS` before a single word of the real answer existed.
"Keep the answer short" was effectively being read as "give the model less room to think,"
and the resulting truncation looked exactly like a refusal from the outside.

**Fix.** [`src/vasool/llm/client.py`](src/vasool/llm/client.py) now raises every request to a
`MIN_OUTPUT_TOKENS = 1024` floor regardless of what the caller passed in, and separates
`finish_reason` / `finishReason` truncation (`"length"` / `"MAX_TOKENS"`) into its own
`reason="max_tokens"`, kept distinct from an actual refusal.

## 6. `make up` silently served stub mode — three separate times

**Symptom.** `.env` had real `GROQ_API_KEY` / `VASOOL_LLM=live` values sitting in it, and the
dashboard kept showing `degraded: llm` / stub-mode behavior regardless. I debugged this,
"fixed" it by manually `source .env`-ing first, and then ran straight into it again in a later
session. Twice.

**Root cause.** `make up` ran `uvicorn` directly. Make targets don't inherit whatever's in the
shell's sourced environment unless a `.env` file is explicitly loaded into the recipe, and
nothing in the Makefile did that — so `VASOOL_LLM` and both API keys were simply absent from
the process, even though they were sitting right there in `.env` on disk. There was no error
anywhere, either: `is_stub_mode()` treats a missing `VASOOL_LLM` exactly like an explicit
`stub`, by design (`.env.example` says as much: "no network call and no key required"), so the
fallback looked like a working system quietly giving a boring answer, not like a bug at all.

**Fix.** Third time was the charm, and I fixed the actual cause instead of the workaround: the
environment loading moved into the Makefile itself, guarded for the no-`.env` case, since a
fresh clone still has to work with nothing set. See [`Makefile:5-13`](Makefile):

```makefile
ifneq (,$(wildcard .env))
include .env
export
endif
```

I verified both branches after that — `.env` present and `.env` absent — actually produce the
right mode, rather than just assuming the guard was enough.
