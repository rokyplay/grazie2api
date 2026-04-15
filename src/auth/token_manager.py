"""TokenManager: refresh_token -> id_token -> JWT lifecycle."""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from src.auth.pkce import decode_jwt_payload
from src.config import Settings

log = logging.getLogger("grazie2api.token")


class TokenManager:
    """Manages JB tokens: refresh_token -> id_token -> JWT.

    Thread-safe via asyncio.Lock.
    """

    def __init__(
        self,
        refresh_token: str,
        license_id: str,
        settings: Settings,
        id_token: str = "",
        jwt: str = "",
        owner_id: str = "",
        on_refresh_token_update: "callable | None" = None,
    ) -> None:
        self.refresh_token = refresh_token
        self.license_id = license_id
        self.settings = settings
        self.id_token = id_token
        self.jwt = jwt
        self.jwt_expires: float = 0
        self.id_token_expires: float = 0
        self.owner_id: str = owner_id
        self._lock = asyncio.Lock()
        self._client: httpx.AsyncClient | None = None
        self._on_refresh_token_update = on_refresh_token_update

        if jwt:
            claims = decode_jwt_payload(jwt)
            self.jwt_expires = claims.get("exp", 0)

        if id_token:
            claims = decode_jwt_payload(id_token)
            self.id_token_expires = claims.get("exp", time.time() + 3600)

    def set_client(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def _refresh_id_token(self) -> None:
        """Refresh id_token using refresh_token."""
        assert self._client is not None
        log.info("Refreshing id_token for %s with RT=%s...", self.owner_id, self.refresh_token[:20])
        resp = await self._client.post(
            self.settings.urls.hub_token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.settings.auth.client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        resp.raise_for_status()
        body = resp.json()

        self.id_token = body.get("id_token") or body.get("access_token", "")
        new_rt = body.get("refresh_token")
        if new_rt:
            old_rt = self.refresh_token
            self.refresh_token = new_rt
            if self._on_refresh_token_update and old_rt != new_rt:
                self._on_refresh_token_update(self.owner_id, new_rt)

        expires_in = body.get("expires_in", 3600)
        self.id_token_expires = time.time() + expires_in
        log.info("id_token refreshed (expires_in=%ds)", expires_in)

    async def _refresh_jwt(self) -> None:
        """Get a new JWT from Grazie provide-access."""
        assert self._client is not None
        log.info("Refreshing JWT (license=%s)...", self.license_id)
        resp = await self._client.post(
            self.settings.urls.jwt_url,
            json={"licenseId": self.license_id},
            headers={
                "Authorization": f"Bearer {self.id_token}",
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        token = body.get("token")
        if not token:
            raise ValueError(f"provide-access returned no token field: {list(body.keys())}")
        self.jwt = token

        claims = decode_jwt_payload(self.jwt)
        self.jwt_expires = claims.get("exp", time.time() + 82800)

        log.info(
            "JWT refreshed (expires at %s)",
            time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.jwt_expires)),
        )

    async def ensure_valid_jwt(self) -> str:
        """Return a valid JWT, refreshing tokens as needed."""
        async with self._lock:
            now = time.time()
            if not self.id_token or now >= self.id_token_expires - self.settings.tokens.access_token_margin_seconds:
                await self._refresh_id_token()
            if not self.jwt or now >= self.jwt_expires - self.settings.tokens.jwt_margin_seconds:
                await self._refresh_jwt()
            return self.jwt
