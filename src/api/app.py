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
from src.db.database import init_db, close_db
from src.stats.recorder import StatsRecorder
from src.quota.refresher import quota_refresher_loop, load_quota_cache
from src.cron.jwt_refresh import jwt_refresh_loop

log = logging.getLogger("grazie2api.app")


@dataclass
class AppState:
    """Central application state."""
    settings: Settings = field(default_factory=Settings)
    http_client: httpx.AsyncClient | None = None
    api_key: str = ""
    pool: CredentialPool | None = None
    user_pools: dict = field(default_factory=dict)  # jb_api_key -> CredentialPool
    stats: StatsRecorder | None = None
    strategy: str = "round_robin"
    _quota_task: asyncio.Task | None = None
    _jwt_refresh_task: asyncio.Task | None = None


# Singleton state
state = AppState()


@asynccontextmanager
async def lifespan(application: FastAPI):
    # Initialize SQLite database
    await init_db(state.settings)

    state.http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=15, read=300, write=30, pool=30),
        trust_env=False,
        verify=True,
    )
    if state.pool is not None:
        state.pool.attach_client(state.http_client)
        load_quota_cache(state.pool, state.settings)
    # Load per-user isolated pools
    _load_per_user_pools(state)
    if state.stats is not None:
        await state.stats.start()
    if state.pool is not None and state.http_client is not None:
        state._quota_task = asyncio.create_task(
            quota_refresher_loop(state.pool, state.http_client, state.settings)
        )
    # Start per-user JWT refresh cron (refreshes jb_credentials JWTs in SQLite)
    if state.http_client is not None:
        state._jwt_refresh_task = asyncio.create_task(
            jwt_refresh_loop(state.settings, state.http_client, interval=600)
        )
    log.info(
        "grazie2api started (pool=%d, strategy=%s)",
        state.pool.count() if state.pool else 0, state.strategy,
    )
    yield
    if state._jwt_refresh_task is not None:
        state._jwt_refresh_task.cancel()
        try:
            await state._jwt_refresh_task
        except (asyncio.CancelledError, Exception):
            pass
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
    await close_db()
    log.info("grazie2api stopped")


def _load_per_user_pools(st: AppState) -> None:
    """Load per-user credential pools from per-user-pool.json."""
    import json
    pool_file = st.settings.config_home / "per-user-pool.json"
    if not pool_file.exists():
        log.info("No per-user-pool.json found, skipping")
        return
    try:
        data = json.loads(pool_file.read_text(encoding="utf-8"))
        for api_key, creds in data.items():
            cred_dicts = []
            for c in creds:
                cred_dicts.append({
                    "id": c.get("id", ""),
                    "label": c.get("jb_email", ""),
                    "refresh_token": c.get("refresh_token", ""),
                    "license_id": c.get("license_id", ""),
                    "user_email": c.get("jb_email", ""),
                })
            pool = CredentialPool(cred_dicts, st.settings)
            if st.http_client:
                pool.attach_client(st.http_client)
            st.user_pools[api_key] = pool
        log.info("Loaded %d per-user pools (%d total credentials)",
                 len(st.user_pools), sum(p.count() for p in st.user_pools.values()))
    except Exception as e:
        log.error("Failed to load per-user-pool.json: %s", e)


def get_pool_for_key(api_key: str) -> CredentialPool | None:
    """Get the credential pool for a given API key. Returns None if key not found."""
    return state.user_pools.get(api_key)


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
