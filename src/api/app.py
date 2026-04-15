"""FastAPI application factory and AppState."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import Settings
from src.credentials.pool import CredentialPool
from src.stats.recorder import StatsRecorder
from src.quota.refresher import quota_refresher_loop, load_quota_cache

log = logging.getLogger("grazie2api.app")


@dataclass
class AppState:
    """Central application state."""
    settings: Settings = field(default_factory=Settings)
    http_client: httpx.AsyncClient | None = None
    api_key: str = ""
    pool: CredentialPool | None = None
    stats: StatsRecorder | None = None
    strategy: str = "round_robin"
    _quota_task: asyncio.Task | None = None


# Singleton state
state = AppState()


@asynccontextmanager
async def lifespan(application: FastAPI):
    state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=300, write=30, pool=30),
        trust_env=False,
        verify=True,
    )
    if state.pool is not None:
        state.pool.attach_client(state.http_client)
        load_quota_cache(state.pool, state.settings)
    if state.stats is not None:
        await state.stats.start()
    if state.pool is not None and state.http_client is not None:
        state._quota_task = asyncio.create_task(
            quota_refresher_loop(state.pool, state.http_client, state.settings)
        )
    log.info(
        "grazie2api started (pool=%d, strategy=%s)",
        state.pool.count() if state.pool else 0, state.strategy,
    )
    yield
    if state._quota_task is not None:
        state._quota_task.cancel()
        try:
            await state._quota_task
        except (asyncio.CancelledError, Exception):
            pass
    if state.stats is not None:
        await state.stats.stop()
    if state.http_client:
        await state.http_client.aclose()
    log.info("grazie2api stopped")


def create_app() -> FastAPI:
    """Create and return the FastAPI application with all routes registered."""
    application = FastAPI(
        title="grazie2api",
        description="Grazie AI API proxy — OpenAI / Anthropic / Responses compatible",
        version="1.0.0",
        lifespan=lifespan,
    )

    application.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    from src.api.routes_proxy import router as proxy_router
    from src.api.routes_credentials import router as credentials_router

    application.include_router(proxy_router)
    application.include_router(credentials_router)

    return application
