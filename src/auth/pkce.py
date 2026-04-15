"""PKCE generation and JWT payload decoding (no signature verification)."""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import string
import uuid


def generate_pkce() -> tuple[str, str, str]:
    """Generate PKCE code_verifier, code_challenge (S256), and state."""
    valid_chars = string.ascii_letters + string.digits + "-._~"
    code_verifier = "".join(secrets.choice(valid_chars) for _ in range(64))
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    state = str(uuid.uuid4())
    return code_verifier, code_challenge, state


def decode_jwt_payload(token: str) -> dict:
    """Decode a JWT payload via base64 (NO signature verification).

    WARNING: This function does NOT verify the JWT signature. It must only be
    used for display purposes (name, email) and expiry scheduling. Do NOT use
    the returned claims for any security-sensitive decisions (authorization,
    access control, admin whitelisting, etc.).
    """
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(payload))
    except Exception:
        return {}
