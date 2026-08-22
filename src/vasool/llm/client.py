"""The one place an LLM call can originate from — every other module that wants a completion
goes through LLMClient, never `anthropic` directly. Every call returns FallbackTriggered
instead of raising (CLAUDE.md: "Never let a failing external dependency take the system
down... never silently swallow an exception. Log structured, set the degraded flag, take the
documented fallback path, and surface it"), so callers never need their own try/except around
a network call.

VASOOL_LLM=stub (default, no key required) skips the network entirely: every call resolves to
FallbackTriggered("stub_mode") immediately. This is what lets `make bench` / `make test` run
offline — a reviewer without an ANTHROPIC_API_KEY still sees the number (.env.example). Set
VASOOL_LLM=anthropic to actually call the API.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TypeVar

import anthropic
from pydantic import BaseModel

from vasool.logging import get_logger

logger = get_logger(__name__)

MODEL = "claude-opus-5"

# BUILD_PLAN.md Phase 7: "timeout, single retry." The SDK's own retry/timeout machinery
# already does this correctly (exponential backoff on 429/5xx/connection errors) -- no reason
# to hand-run a second retry loop on top of it.
REQUEST_TIMEOUT_SECONDS = 20.0
MAX_RETRIES = 1

T = TypeVar("T", bound=BaseModel)


@dataclass(frozen=True)
class FallbackTriggered:
    """Returned, never raised. `reason` is logged by the caller's own fallback path, not just
    here -- this dataclass only carries *why the LLM call itself* didn't produce an answer.
    """

    reason: str


def _stub_mode() -> bool:
    return os.environ.get("VASOOL_LLM", "stub") != "anthropic"


class LLMClient:
    def __init__(self) -> None:
        self._client: anthropic.Anthropic | None = (
            None if _stub_mode() else anthropic.Anthropic(timeout=REQUEST_TIMEOUT_SECONDS, max_retries=MAX_RETRIES)
        )

    def parse(self, *, system: str, user: str, output_format: type[T], max_tokens: int = 1024) -> T | FallbackTriggered:
        """Strict JSON output: `output_format` is a Pydantic model, `response.parsed_output`
        is a validated instance of it. Used wherever the caller needs a typed, enum-constrained
        answer (diagnose/llm_fallback.py, llm/policy_compiler.py) rather than free text.
        """
        if self._client is None:
            return FallbackTriggered(reason="stub_mode")
        try:
            response = self._client.messages.parse(
                model=MODEL, max_tokens=max_tokens, system=system,
                messages=[{"role": "user", "content": user}], output_format=output_format,
            )
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        if response.stop_reason == "refusal":
            logger.warning("llm_refusal")
            return FallbackTriggered(reason="refusal")
        if response.parsed_output is None:
            logger.warning("llm_parse_failed")
            return FallbackTriggered(reason="parse_failed")
        return response.parsed_output

    def complete(self, *, system: str, user: str, max_tokens: int = 1024) -> str | FallbackTriggered:
        """Free text, for tasks with no fixed schema (llm/narrative.py, audit/explain.py)."""
        if self._client is None:
            return FallbackTriggered(reason="stub_mode")
        try:
            response = self._client.messages.create(
                model=MODEL, max_tokens=max_tokens, system=system, messages=[{"role": "user", "content": user}],
            )
        except Exception as exc:  # noqa: BLE001 -- this boundary must never raise; see module docstring
            return self._fallback_for(exc)
        if response.stop_reason == "refusal":
            logger.warning("llm_refusal")
            return FallbackTriggered(reason="refusal")
        text = next((block.text for block in response.content if block.type == "text"), None)
        if not text:
            logger.warning("llm_empty_response")
            return FallbackTriggered(reason="empty_response")
        return text

    @staticmethod
    def _fallback_for(exc: Exception) -> FallbackTriggered:
        # Most-specific first (shared/error-codes.md's chain), each logged with enough detail
        # to distinguish "back off and retry later" from "this will never work" without ever
        # re-raising into the caller.
        if isinstance(exc, anthropic.RateLimitError):
            logger.warning("llm_rate_limited", error=str(exc))
            return FallbackTriggered(reason="rate_limited")
        if isinstance(exc, anthropic.APITimeoutError):
            logger.warning("llm_timeout", error=str(exc))
            return FallbackTriggered(reason="timeout")
        if isinstance(exc, anthropic.AuthenticationError):
            logger.warning("llm_authentication_failed", error=str(exc))
            return FallbackTriggered(reason="authentication_failed")
        if isinstance(exc, anthropic.APIStatusError):
            logger.warning("llm_api_status_error", status_code=exc.status_code, error=str(exc))
            return FallbackTriggered(reason="api_status_error")
        if isinstance(exc, anthropic.APIConnectionError):
            logger.warning("llm_connection_error", error=str(exc))
            return FallbackTriggered(reason="connection_error")
        if isinstance(exc, anthropic.AnthropicError):
            logger.warning("llm_anthropic_error", error=str(exc))
            return FallbackTriggered(reason="anthropic_error")
        # Anything else (e.g. an unexpected error surfaced through .parse()'s validation path)
        # -- still never escapes this boundary.
        logger.warning("llm_unexpected_error", error=str(exc), error_type=type(exc).__name__)
        return FallbackTriggered(reason="unexpected_error")
