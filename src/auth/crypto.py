"""Cryptographic utilities: SHA-256, timing-safe comparison, API key generation."""

from __future__ import annotations

import hashlib
import hmac
import secrets


def sha256_hex(data: str) -> str:
    """Return the hex-encoded SHA-256 digest of *data*."""
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def timing_safe_equal(a: str, b: str) -> bool:
    """Constant-time string comparison to prevent timing attacks."""
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def create_api_key(prefix: str = "dk-") -> str:
    """Generate a new API key with the given prefix (default ``dk-``)."""
    safe_prefix = prefix if prefix.endswith("-") else f"{prefix}-"
    return f"{safe_prefix}{secrets.token_hex(24)}"
