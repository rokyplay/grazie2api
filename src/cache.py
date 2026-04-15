"""In-memory cache with TTL support.

Replaces Cloudflare KV for local operation.
Thread-safe via asyncio (single-threaded event loop).

Usage:
    cache = MemoryCache()
    await cache.put("key", "value", ttl=300)
    val = await cache.get("key")
    await cache.delete("key")
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("grazie2api.cache")


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float  # 0 = no expiry


@dataclass
class MemoryCache:
    """Simple in-memory key-value cache with TTL.

    Designed to replace Cloudflare KV for:
    - Model list caching
    - Rate limit counters
    - Session tokens
    - Temporary state
    """

    _store: dict[str, _CacheEntry] = field(default_factory=dict)
    _max_size: int = 10000  # Prevent unbounded growth

    async def get(self, key: str) -> Any | None:
        """Get a value by key. Returns None if not found or expired."""
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at > 0 and time.monotonic() > entry.expires_at:
            del self._store[key]
            return None
        return entry.value

    async def put(self, key: str, value: Any, ttl: int = 0) -> None:
        """Store a value with optional TTL in seconds. ttl=0 means no expiry."""
        # Evict expired entries if approaching max size
        if len(self._store) >= self._max_size:
            self._evict_expired()
        if len(self._store) >= self._max_size:
            # Still full after eviction — remove oldest entries
            oldest_keys = sorted(
                self._store.keys(),
                key=lambda k: self._store[k].expires_at if self._store[k].expires_at > 0 else float("inf"),
            )
            for k in oldest_keys[: len(oldest_keys) // 4]:
                del self._store[k]

        expires_at = (time.monotonic() + ttl) if ttl > 0 else 0.0
        self._store[key] = _CacheEntry(value=value, expires_at=expires_at)

    async def delete(self, key: str) -> bool:
        """Delete a key. Returns True if it existed."""
        if key in self._store:
            del self._store[key]
            return True
        return False

    async def has(self, key: str) -> bool:
        """Check if a key exists and is not expired."""
        return (await self.get(key)) is not None

    async def clear(self) -> None:
        """Clear all entries."""
        self._store.clear()

    def size(self) -> int:
        """Return the number of entries (including potentially expired ones)."""
        return len(self._store)

    def _evict_expired(self) -> None:
        """Remove all expired entries."""
        now = time.monotonic()
        expired = [
            k for k, v in self._store.items()
            if v.expires_at > 0 and now > v.expires_at
        ]
        for k in expired:
            del self._store[k]
        if expired:
            log.debug("Evicted %d expired cache entries", len(expired))


# Singleton instances for different purposes
model_cache = MemoryCache()      # Model list / profiles caching
rate_limit_cache = MemoryCache()  # Rate limiting counters
session_cache = MemoryCache()     # Temporary session state
