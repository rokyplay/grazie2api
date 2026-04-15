"""Auth, body size, and rate-limit middleware helpers."""

from __future__ import annotations

import time
from collections import defaultdict

from fastapi import HTTPException, Request

from src.api.app import state


def check_auth(
    authorization: str | None = None,
    x_api_key: str | None = None,
) -> None:
    """Validate auth via Bearer token or x-api-key."""
    if not state.api_key:
        return  # No api_key configured = open access (local self-service mode)
    token = None
    if authorization:
        token = authorization.removeprefix("Bearer ").strip()
    elif x_api_key:
        token = x_api_key.strip()
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


# Global-pool per-user limiter: 1 request per 60 seconds per API key
global_pool_limiter = _RateLimiter(max_requests=1, window_seconds=60)
