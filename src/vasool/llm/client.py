"""The one place an LLM call can originate from — every other module that wants a completion
goes through LLMClient, never Groq's or Gemini's API directly. Every call returns
FallbackTriggered instead of raising (CLAUDE.md: "Never let a failing external dependency take
the system down... never silently swallow an exception. Log structured, set the degraded flag,
take the documented fallback path, and surface it"), so callers never need their own
try/except around a network call.

VASOOL_LLM=stub (default, no key required) skips the network entirely: every call resolves to
FallbackTriggered("stub_mode") immediately. This is what lets `make bench` / `make test` run
offline -- a reviewer without any key still sees the number (.env.example). Set VASOOL_LLM=live
to actually call out.

Two providers, not one, because a single free-tier key turned out to be too fragile to demo on
(gemini-3.6-flash's free daily quota is 20 requests -- found by burning through it during
testing, not from any published number). Groq is tried first (its published per-model RPM/RPD
are far more generous, and it's fast); Gemini is the fallback on any Groq failure that isn't
stub_mode. If both fail, the caller's own deterministic fallback (a template, a rule, refusing
outright) takes over -- this module never invents an answer neither provider actually gave.

Both talked to over their plain REST APIs via httpx rather than an SDK -- httpx is already in
CLAUDE.md's approved dependency list, google-generativeai and groq/openai aren't, and this is
the only place in the codebase that needs to know either provider's request/response shape.
Groq's API is OpenAI-compatible (chat/completions, choices[0].message.content); Gemini's is not
(generateContent, candidates[0].content.parts[].text) -- see the provider-specific methods
below for what was actually confirmed against each live endpoint, and when.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from vasool.logging import get_logger

logger = get_logger(__name__)

GROQ_API_KEY_ENV = "GROQ_API_KEY"
GROQ_BASE_URL = "https://api.groq.com/openai/v1"
# llama-3.1-8b-instant and llama-3.3-70b-versatile, the models most guides still cite, were
# both deprecated and removed 16 Aug 2026 -- confirmed by listing this key's actual available
# models (GET /v1/models), not by trusting a guide. gpt-oss-20b is real, active, and confirmed
# working end to end (chat + json_schema structured output) against the live API, 25 Aug 2026.
GROQ_MODEL = "openai/gpt-oss-20b"

GEMINI_API_KEY_ENV = "GEMINI_API_KEY"
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# gemini-2.5-flash returns a hard 404 for keys created after its new-user cutoff ("no longer
# available to new users") -- confirmed directly against the live API, 25 Aug 2026, not from
# docs. Google's own error message names gemini-3.6-flash as the replacement; confirmed
# working with a real call before landing here. Its structured-output shape didn't match the
# docs either: generationConfig.responseFormat.text.{mimeType,schema} 400s, the flat
# generationConfig.responseMimeType / .responseSchema is what the live API actually wants.
GEMINI_MODEL = "gemini-3.6-flash"

# BUILD_PLAN.md Phase 7: "timeout, single retry." The anthropic SDK used to do this itself;
# httpx doesn't retry by default, so this is now a one-line manual retry in _post(), only on
# a timeout -- a 4xx/5xx is never retried, since retrying an auth failure or bad request
# doesn't help.
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 1

# Both providers' models think before they answer, and thinking tokens are billed against the
# same output-token budget as the visible response -- confirmed directly against both live
# APIs, 25 Aug 2026: a caller-requested budget of 200 burned ~190 tokens on invisible Gemini
# thinking alone and hit MAX_TOKENS before producing a single word of the actual answer. A
# caller asking for a short reply doesn't mean "give the model less room to think"; it means
# "keep the visible answer short," so every request's floor is raised here regardless of what
# the caller passed.
MIN_OUTPUT_TOKENS = 1024

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class FallbackTriggered:
    """Returned, never raised. `reason` is logged by the caller's own fallback path, not just
    here -- this dataclass only carries *why the LLM call itself* didn't produce an answer.
    """

    reason: str


def is_stub_mode() -> bool:
    """Public, not just an LLMClient implementation detail: api/dashboard.py reads this
    directly to render the `llm` degraded-mode badge.
    """
    return os.environ.get("VASOOL_LLM", "stub") != "live"


class LLMClient:
    def __init__(self) -> None:
        live = not is_stub_mode()
        self._groq: httpx.Client | None = (
            httpx.Client(
                base_url=GROQ_BASE_URL,
                headers={"Authorization": f"Bearer {os.environ.get(GROQ_API_KEY_ENV, '')}"},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if live else None
        )
        self._gemini: httpx.Client | None = (
            httpx.Client(
                base_url=GEMINI_BASE_URL,
                headers={"x-goog-api-key": os.environ.get(GEMINI_API_KEY_ENV, "")},
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            if live else None
        )

    def parse(self, *, system: str, user: str, output_format: type[T], max_tokens: int = 1024) -> T | FallbackTriggered:
        """Strict JSON output: `output_format` is a Pydantic model, validated from the
        provider's response text against its own JSON schema. Used wherever the caller needs a
        typed, enum-constrained answer (diagnose/llm_fallback.py, llm/policy_compiler.py)
        rather than free text. Tries Groq, falls back to Gemini on any Groq failure.
        """
        if self._groq is None or self._gemini is None:
            return FallbackTriggered(reason="stub_mode")
        result = self._parse_groq(system, user, output_format, max_tokens)
        if isinstance(result, FallbackTriggered):
            logger.info("llm_provider_fallback", primary="groq", primary_reason=result.reason)
            result = self._parse_gemini(system, user, output_format, max_tokens)
        return result

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str | FallbackTriggered:
        """Free text, for tasks with no fixed schema (llm/narrative.py, audit/explain.py).
        Tries Groq, falls back to Gemini on any Groq failure.
        """
        if self._groq is None or self._gemini is None:
            return FallbackTriggered(reason="stub_mode")
        result = self._complete_groq(system, user, max_tokens)
        if isinstance(result, FallbackTriggered):
            logger.info("llm_provider_fallback", primary="groq", primary_reason=result.reason)
            result = self._complete_gemini(system, user, max_tokens)
        return result

    # -- Groq (OpenAI-compatible) --------------------------------------------------------

    def _complete_groq(self, system: str, user: str, max_tokens: int) -> str | FallbackTriggered:
        assert self._groq is not None  # only called from complete()/parse(), which already guard this
        body = self._groq_body(system, user, max_tokens)
        try:
            data = self._post(self._groq, "/chat/completions", body)
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        return self._extract_text_groq(data)

    def _parse_groq(self, system: str, user: str, output_format: type[T], max_tokens: int) -> T | FallbackTriggered:
        assert self._groq is not None  # only called from complete()/parse(), which already guard this
        body = self._groq_body(system, user, max_tokens, schema=output_format.model_json_schema())
        try:
            data = self._post(self._groq, "/chat/completions", body)
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        return self._validate(self._extract_text_groq(data), output_format, "groq")

    @staticmethod
    def _groq_body(system: str, user: str, max_tokens: int, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": GROQ_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max(max_tokens, MIN_OUTPUT_TOKENS),
        }
        if schema is not None:
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
        return body

    @staticmethod
    def _extract_text_groq(data: dict[str, Any]) -> str | FallbackTriggered:
        choices = data.get("choices") or []
        if not choices:
            logger.warning("llm_empty_response", provider="groq")
            return FallbackTriggered(reason="empty_response")
        finish_reason = choices[0].get("finish_reason")
        if finish_reason == "length":
            # Same non-refusal truncation as Gemini's MAX_TOKENS -- see MIN_OUTPUT_TOKENS.
            logger.warning("llm_truncated", provider="groq", finish_reason=finish_reason)
            return FallbackTriggered(reason="max_tokens")
        if finish_reason not in (None, "stop"):
            logger.warning("llm_refusal", provider="groq", finish_reason=finish_reason)
            return FallbackTriggered(reason="refusal")
        text = choices[0].get("message", {}).get("content")
        if not text:
            logger.warning("llm_empty_response", provider="groq")
            return FallbackTriggered(reason="empty_response")
        return str(text)

    # -- Gemini ---------------------------------------------------------------------------

    def _complete_gemini(self, system: str, user: str, max_tokens: int) -> str | FallbackTriggered:
        assert self._gemini is not None  # only called from complete()/parse(), which already guard this
        body = self._gemini_body(system, user, max_tokens)
        try:
            data = self._post(self._gemini, f"/models/{GEMINI_MODEL}:generateContent", body)
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        return self._extract_text_gemini(data)

    def _parse_gemini(self, system: str, user: str, output_format: type[T], max_tokens: int) -> T | FallbackTriggered:
        assert self._gemini is not None  # only called from complete()/parse(), which already guard this
        body = self._gemini_body(system, user, max_tokens, schema=output_format.model_json_schema())
        try:
            data = self._post(self._gemini, f"/models/{GEMINI_MODEL}:generateContent", body)
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        return self._validate(self._extract_text_gemini(data), output_format, "gemini")

    @staticmethod
    def _gemini_body(system: str, user: str, max_tokens: int, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        generation_config: dict[str, Any] = {"maxOutputTokens": max(max_tokens, MIN_OUTPUT_TOKENS)}
        if schema is not None:
            generation_config["responseMimeType"] = "application/json"
            generation_config["responseSchema"] = schema
        return {
            "contents": [{"parts": [{"text": user}]}],
            "systemInstruction": {"parts": [{"text": system}]},
            "generationConfig": generation_config,
        }

    @staticmethod
    def _extract_text_gemini(data: dict[str, Any]) -> str | FallbackTriggered:
        candidates = data.get("candidates") or []
        if not candidates:
            logger.warning("llm_empty_response", provider="gemini")
            return FallbackTriggered(reason="empty_response")
        finish_reason = candidates[0].get("finishReason")
        if finish_reason == "MAX_TOKENS":
            logger.warning("llm_truncated", provider="gemini", finish_reason=finish_reason)
            return FallbackTriggered(reason="max_tokens")
        if finish_reason not in (None, "STOP"):
            logger.warning("llm_refusal", provider="gemini", finish_reason=finish_reason)
            return FallbackTriggered(reason="refusal")
        parts = candidates[0].get("content", {}).get("parts") or []
        text = next((p["text"] for p in parts if "text" in p), None)
        if not text:
            logger.warning("llm_empty_response", provider="gemini")
            return FallbackTriggered(reason="empty_response")
        return str(text)

    # -- shared -----------------------------------------------------------------------------

    @staticmethod
    def _validate(text: str | FallbackTriggered, output_format: type[T], provider: str) -> T | FallbackTriggered:
        if isinstance(text, FallbackTriggered):
            return text
        try:
            return output_format.model_validate_json(text)
        except Exception:  # noqa: BLE001 -- malformed JSON from the model is a fallback trigger, not a crash
            logger.warning("llm_parse_failed", provider=provider)
            return FallbackTriggered(reason="parse_failed")

    @staticmethod
    def _post(client: httpx.Client, path: str, body: dict[str, Any]) -> dict[str, Any]:
        try:
            response = client.post(path, json=body)
        except httpx.TimeoutException:
            response = client.post(path, json=body)  # the one retry MAX_RETRIES documents
        response.raise_for_status()
        result: dict[str, Any] = response.json()
        return result

    @staticmethod
    def _fallback_for(exc: Exception) -> FallbackTriggered:
        # Most-specific first, each logged with enough detail to distinguish "back off and
        # retry later" from "this will never work" without ever re-raising into the caller.
        if isinstance(exc, httpx.TimeoutException):
            logger.warning("llm_timeout", error=str(exc))
            return FallbackTriggered(reason="timeout")
        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if status == 429:
                logger.warning("llm_rate_limited", error=str(exc))
                return FallbackTriggered(reason="rate_limited")
            if status in (401, 403):
                logger.warning("llm_authentication_failed", error=str(exc))
                return FallbackTriggered(reason="authentication_failed")
            logger.warning("llm_api_status_error", status_code=status, error=str(exc))
            return FallbackTriggered(reason="api_status_error")
        if isinstance(exc, httpx.HTTPError):
            logger.warning("llm_connection_error", error=str(exc))
            return FallbackTriggered(reason="connection_error")
        # Anything else (e.g. an unexpected error surfaced through .parse()'s validation path)
        # -- still never escapes this boundary.
        logger.warning("llm_unexpected_error", error=str(exc), error_type=type(exc).__name__)
        return FallbackTriggered(reason="unexpected_error")
