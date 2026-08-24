# Engineering log

Six real bugs hit while building this, kept here because a benchmark number is easy to fake
and a debugged incident isn't. Each one is symptom → root cause → fix, with the file that
proves it. Nothing here is illustrative; every one of these actually happened, on the dates
shown.

## 1. A naive datetime slipped past `AwareDatetime` after a SQLite round-trip

**Symptom.** `/demo/explain` — the "explain this decision in English" button on the audit row
— threw a 500: `pydantic_core.ValidationError: decided_at Input should have timezone info`.
The row it was reading had just been written by the same process, with a real
timezone-aware `datetime`.

**Root cause.** SQLite has no native timestamp type; SQLModel stores `datetime` as text and
reads it back **naive**, regardless of what was written. `Decision.decided_at` is typed
`AwareDatetime` (invariant 9: "a naive datetime anywhere is a bug"), so the object built
straight from the row failed validation the moment it was reconstructed for anything other
than raw column access.

**Fix.** [`src/vasool/api/dashboard.py`](src/vasool/api/dashboard.py) `_reconstruct_decision`
reattaches UTC explicitly on the way out of SQLite — invariant 9 already establishes that
anything stored is UTC, so this isn't a guess:

```python
# SQLite round-trips a datetime as naive regardless of what was written -- invariant 9
# ("store UTC") means this is always UTC on the way back out, so we reattach it here
# rather than let a naive datetime reach Decision's AwareDatetime field.
decided_at = row.decided_at.replace(tzinfo=UTC)
```

## 2. Gemini's own docs describe a request shape that 400s on the live API

**Symptom.** Structured-output calls to Gemini failed immediately with `Invalid value at
'generation_config.response_format.text.mime_type'` — a 400, not a fallback-worthy transient
error.

**Root cause.** The documented shape for constrained JSON output is
`generationConfig.responseFormat.text.{mimeType,schema}`. That's what the reference docs show.
The live `v1beta` endpoint rejects it outright.

**Fix.** Found the accepted shape by testing directly against the real endpoint rather than
re-reading the docs harder — a flat `generationConfig.responseMimeType` /
`.responseSchema`, no nested `text` object. See
[`src/vasool/llm/client.py:55-56`](src/vasool/llm/client.py):

```python
# docs either: generationConfig.responseFormat.text.{mimeType,schema} 400s, the flat
# generationConfig.responseMimeType / .responseSchema is what the live API actually wants.
```

## 3. `gemini-2.5-flash`, the model every guide names, 404s for new keys

**Symptom.** Every call with the widely-documented `gemini-2.5-flash` model name returned a
hard 404.

**Root cause.** Google stopped issuing `gemini-2.5-flash` access to API keys created after a
cutoff date — "no longer available to new users," stated in the 404 body itself, not in any
docs page. A key created today gets a model name that every published guide still confidently
recommends, and it simply doesn't work.

**Fix.** Google's own error names the replacement. Switched to `gemini-3.6-flash`, confirmed
with a real successful call before adopting it — see
[`src/vasool/llm/client.py:51-57`](src/vasool/llm/client.py).

## 4. Two Groq model names in every guide are dead

**Symptom.** `llama-3.1-8b-instant` and `llama-3.3-70b-versatile` — the two models nearly
every Groq quickstart still cites — both failed.

**Root cause.** Both were deprecated and removed 16 Aug 2026. Guides and cached blog posts
don't know that yet.

**Fix.** Queried the key's own available models directly (`GET /openai/v1/models`) instead of
trusting a guide, found `openai/gpt-oss-20b`, and confirmed it end-to-end — plain chat and
`json_schema` structured output — against the live API before adopting it. See
[`src/vasool/llm/client.py:43-47`](src/vasool/llm/client.py).

## 5. `MAX_TOKENS` was silently indistinguishable from a genuine refusal

**Symptom.** Calls with a deliberately tight token budget (a caller asking for a short reply)
came back empty, and the code was treating that the same as the model refusing to answer.

**Root cause.** Both Groq's and Gemini's models spend hidden "thinking" tokens before
producing any visible output, and those thinking tokens are billed against the *same*
output-token budget the caller requested. A budget of 200 burned roughly 190 tokens on
invisible Gemini reasoning alone and hit `MAX_TOKENS` before a single word of the real answer
existed. "Keep the answer short" was being read as "give the model less room to think,"
which produced truncation that looked exactly like a refusal.

**Fix.** [`src/vasool/llm/client.py`](src/vasool/llm/client.py) raises every request to a
`MIN_OUTPUT_TOKENS = 1024` floor regardless of what the caller passed, and separates
`finish_reason`/`finishReason` truncation (`"length"` / `"MAX_TOKENS"`) into its own
`reason="max_tokens"`, distinct from an actual refusal.

## 6. `make up` silently served stub mode — three separate times

**Symptom.** `.env` had real `GROQ_API_KEY` / `VASOOL_LLM=live` values set, and the dashboard
kept showing `degraded: llm` / stub-mode behavior anyway. This was debugged, "fixed" by
manually `source .env`-ing first, and then hit again in a later session — twice.

**Root cause.** `make up` ran `uvicorn` directly. Make targets don't inherit the shell's
sourced environment unless a `.env` file is explicitly loaded into the recipe; nothing in the
Makefile did that, so `VASOOL_LLM` and both API keys were simply absent from the process even
though they were sitting in `.env` on disk. There was no error — `is_stub_mode()` treats a
missing `VASOOL_LLM` exactly like an explicit `stub`, by design (`.env.example`: "no network
call and no key required"), so the fallback looked like a working system serving a boring
answer, not a bug.

**Fix.** Third time was root-cause, not workaround: the environment loading moved into the
Makefile itself, guarded for the no-`.env` case (a fresh clone must still work with nothing
set). See [`Makefile:5-13`](Makefile):

```makefile
ifneq (,$(wildcard .env))
include .env
export
endif
```

Verified both branches — `.env` present and absent — actually produce the right mode rather
than assuming the guard was sufficient.
