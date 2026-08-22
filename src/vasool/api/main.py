"""`make up` entrypoint — BUILD_PLAN.md Phase 8 accept: "dashboard at localhost:8000 with
seeded data." Builds the seed cohort and live-mode store once at startup and hangs them on
app.state so every router (dashboard, inspector, webhooks, admin) reads the same instances.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from vasool.api import admin, dashboard, inspector, webhooks
from vasool.api.seed import build_seed_data
from vasool.api.store import LiveStore
from vasool.llm.client import LLMClient
from vasool.logging import configure_logging

DATABASE_URL_ENV = "DATABASE_URL"
DEFAULT_DB_PATH = "vasool_live.db"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    app.state.seed_data = build_seed_data()
    app.state.live_store = LiveStore(os.environ.get(DATABASE_URL_ENV) or DEFAULT_DB_PATH)
    app.state.llm_client = LLMClient()
    yield


app = FastAPI(title="Vasool", lifespan=lifespan)
app.include_router(dashboard.router)
app.include_router(inspector.router)
app.include_router(webhooks.router)
app.include_router(admin.router)
