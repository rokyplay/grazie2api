"""CredentialEntry: runtime wrapper around a single credential."""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any

import httpx

from src.auth.token_manager import TokenManager
from src.config import Settings

log = logging.getLogger("grazie2api.cred")


class CredentialEntry:
    """Runtime wrapper around a single credential.

    Holds:
      - static metadata (id, label, refresh_token, license_id)
      - TokenManager for JWT refresh
      - quota snapshot {current, maximum, until}
      - cooldown_until (seconds since epoch)
    """

    def __init__(self, data: dict, settings: Settings) -> None:
        self.id: str = data.get("id") or f"cred-{uuid.uuid4().hex[:8]}"
        self.label: str = data.get("label") or self.id
        self.refresh_token: str = data["refresh_token"]
        self.license_id: str = data.get("license_id", "") or ""
        self.user_email: str = data.get("user_email") or ""
        self.user_name: str = data.get("user_name") or ""
        self.added_at: int = data.get("added_at") or int(time.time())
        self.settings = settings

        from src.credentials.storage import update_multi_refresh_token

        def _on_rt_update(owner_id: str, new_rt: str) -> None:
            update_multi_refresh_token(owner_id, new_rt, settings)

        self.token_manager = TokenManager(
            refresh_token=self.refresh_token,
            license_id=self.license_id,
            settings=settings,
            owner_id=self.id,
            on_refresh_token_update=_on_rt_update,
        )
        self.quota: dict[str, Any] = {}
        self.quota_fetched_at: float = 0
        self.cooldown_until: float = 0
        self.last_error: str = ""

    def attach_client(self, client: httpx.AsyncClient) -> None:
        self.token_manager.set_client(client)

    def is_available(self) -> bool:
        if not self.license_id:
            return False
        return time.time() >= self.cooldown_until

    def mark_cooldown(self, seconds: int | None = None, reason: str = "") -> None:
        if seconds is None:
            seconds = self.settings.credentials.cooldown_seconds
        self.cooldown_until = time.time() + seconds
        self.last_error = reason
        log.warning(
            "[cred %s] cooldown for %ds (%s)",
            self.id, seconds, reason or "unspecified",
        )

    def clear_cooldown(self) -> None:
        self.cooldown_until = 0
        self.last_error = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "license_id": self.license_id,
            "user_email": self.user_email,
            "user_name": self.user_name,
            "added_at": self.added_at,
            "quota": self.quota,
            "quota_fetched_at": self.quota_fetched_at,
            "cooldown_until": self.cooldown_until,
            "available": self.is_available(),
            "last_error": self.last_error,
            "jwt_state": "ready" if self.token_manager.jwt else "pending",
        }
