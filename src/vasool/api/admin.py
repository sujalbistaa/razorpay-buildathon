"""/admin/dlq and /admin/dlq/replay — BUILD_DOC.md §7's "poison queue message" chaos
scenario: a webhook whose processing raised lands in the dead-letter table instead of being
dropped or crashing the worker (webhooks.py's `_process_event`); this is where a human looks
at it and, once fixed, replays it.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from vasool.api.store import LiveStore
from vasool.api.webhooks import _dispatch

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@router.get("/dlq", response_class=HTMLResponse)
def list_dead_letters(request: Request) -> HTMLResponse:
    store: LiveStore = request.app.state.live_store
    return templates.TemplateResponse(request, "admin_dlq.html", {"dead_letters": store.list_dead_letters()})


@router.post("/dlq/replay")
def replay_dead_letter(request: Request, dlq_id: int) -> RedirectResponse:
    store: LiveStore = request.app.state.live_store
    row = store.get_dead_letter(dlq_id)
    if row is None:
        raise HTTPException(status_code=404, detail="dead letter not found")

    payload = json.loads(row.payload_json)
    try:
        _dispatch(store, request.app.state.llm_client, row.event_type, payload)
    except Exception as exc:  # noqa: BLE001 -- a replay that fails again must not crash the admin request; it stays in the DLQ for another look
        store.record_dead_letter(row.event_id, row.event_type, payload, error=f"replay failed: {exc}", now=datetime.now(UTC))
    else:
        store.mark_replayed(dlq_id)

    return RedirectResponse(url="/admin/dlq", status_code=303)
