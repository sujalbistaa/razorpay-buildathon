"""/demo/policy/* — live natural-language policy compilation. Invariant 2's third permitted
LLM use ("drafts policy rules for human approval") isn't shown anywhere else on the
dashboard; this exercises the real llm/policy_compiler.py end to end: compile, diff against
the platform default, and the mandatory confirmed=True gate before anything could activate.

Parses the POST body with stdlib urllib rather than FastAPI's Form(), which needs
python-multipart -- not an installed dependency, and CLAUDE.md forbids adding one without
asking for two text fields.
"""

from __future__ import annotations

from urllib.parse import parse_qsl

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from vasool.llm.client import FallbackTriggered
from vasool.llm.policy_compiler import activate, compile_policy_rule

router = APIRouter(prefix="/demo/policy")


async def _form_field(request: Request, name: str) -> str:
    body = await request.body()
    fields = dict(parse_qsl(body.decode("utf-8")))
    return fields.get(name, "").strip()


@router.post("/compile")
async def compile_rule(request: Request) -> RedirectResponse:
    state = request.app.state
    nl_text = await _form_field(request, "nl_text")
    state.demo_policy_activated = False
    if not nl_text:
        state.demo_policy_rule = None
        state.demo_policy_fallback_reason = None
        return RedirectResponse(url="/#policy-panel", status_code=303)

    result = compile_policy_rule(nl_text, state.llm_client, rule_id="demo_rule")
    if isinstance(result, FallbackTriggered):
        # No deterministic fallback here, on purpose: unlike a customer message, there's no
        # safe template for "guess what this arbitrary English sentence meant as a policy
        # rule." Refusing is the correct fallback, not a synthesized answer.
        state.demo_policy_rule = None
        state.demo_policy_fallback_reason = result.reason
    else:
        state.demo_policy_rule = result
        state.demo_policy_fallback_reason = None
    return RedirectResponse(url="/#policy-panel", status_code=303)


@router.post("/approve")
def approve_rule(request: Request) -> RedirectResponse:
    state = request.app.state
    if state.demo_policy_rule is not None:
        activate(state.demo_policy_rule, confirmed=True)
        state.demo_policy_activated = True
    return RedirectResponse(url="/#policy-panel", status_code=303)


@router.post("/discard")
def discard_rule(request: Request) -> RedirectResponse:
    state = request.app.state
    state.demo_policy_rule = None
    state.demo_policy_fallback_reason = None
    state.demo_policy_activated = False
    return RedirectResponse(url="/#policy-panel", status_code=303)
