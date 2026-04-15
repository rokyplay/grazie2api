"""Auth, body size, and rate-limit middleware helpers."""

from __future__ import annotations

import logging
import time
from collections import defaultdict

from fastapi import HTTPException, Request

from src.api.app import state
from src.auth.authenticator import AuthResult, authenticate_api_key

log = logging.getLogger("grazie2api.middleware")


async def check_auth(request: Request) -> AuthResult:
    """Authenticate the request using the multi-key system.

    Supports:
      - sk-* system keys (timing-safe comparison against config)
      - jb-* JB keys (plaintext lookup in users.jb_api_key)
      - Legacy single api_key mode (if system_api_keys is empty)

    Stores the AuthResult on ``request.state.auth`` and returns it.
    Raises HTTPException(401) on failure.
    """
    # --- Legacy mode: single api_key from config (backward compat) -----
    if not state.settings.system_api_keys and state.api_key:
        token = _extract_token_raw(request)
        if not token:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Missing Authorization header or x-api-key", "type": "auth_error"}},
            )
        if token != state.api_key:
            raise HTTPException(
                status_code=401,
                detail={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )
        result = AuthResult(
            ok=True,
            api_key_id="legacy",
            owner_id="admin",
            owner_type="system",
            identity="admin",
            tier="system",
            source="authorization",
        )
        request.state.auth = result
        return result

    # --- Open access mode (no keys configured at all) ------------------
    if not state.settings.system_api_keys and not state.api_key:
        result = AuthResult(
            ok=True,
            api_key_id="open",
            owner_id="anonymous",
            owner_type="system",
            identity="anonymous",
            tier="system",
            source="open",
        )
        request.state.auth = result
        return result

    # --- Multi-key mode: system_api_keys configured --------------------
    from src.db.database import get_db

    headers = {k.lower(): v for k, v in request.headers.items()}
    path = request.url.path

    db = get_db()
    result = await authenticate_api_key(
        db=db,
        headers=headers,
        system_keys=state.settings.system_api_keys,
        path=path,
    )

    if not result.ok:
        raise HTTPException(
            status_code=result.status,
            detail={"error": {"message": result.message, "type": "auth_error", "code": result.code}},
        )

    request.state.auth = result
    return result


def _extract_token_raw(request: Request) -> str:
    """Extract a raw token from Authorization or x-api-key header."""
    auth_header = request.headers.get("authorization", "")
    if auth_header:
        return auth_header.removeprefix("Bearer ").strip()
    x_key = request.headers.get("x-api-key", "")
    if x_key:
        return x_key.strip()
    return ""


def check_body_size(request: Request) -> None:
    """Reject oversized request bodies to prevent memory exhaustion."""
    max_bytes = state.settings.server.max_request_body_bytes
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail={"error": {"message": "Request body too large", "type": "request_too_large"}},
                )
        except ValueError:
            pass


# ---------------------------------------------------------------------------
# Per-user rate limiter (sliding window, in-memory)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """Simple per-key sliding-window rate limiter."""

    def __init__(self, max_requests: int = 1, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, key: str) -> None:
        """Raise 429 if *key* has exceeded the rate limit."""
        now = time.time()
        cutoff = now - self.window
        hits = self._hits[key]
        # Prune old entries
        self._hits[key] = hits = [t for t in hits if t > cutoff]
        if len(hits) >= self.max_requests:
            retry_after = int(hits[0] + self.window - now) + 1
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "message": f"Rate limit: {self.max_requests} req/{self.window}s. Retry after {retry_after}s.",
                        "type": "rate_limit",
                    }
                },
                headers={"Retry-After": str(retry_after)},
            )
        hits.append(now)


# Global-pool per-user limiter: configurable via settings, default 10 req/min
global_pool_limiter = _RateLimiter(max_requests=10, window_seconds=60)
