"""Credential management routes: paste, auto-parse, webhook, list, remove."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse

from src.api.app import state
from src.credentials.entry import CredentialEntry
from src.credentials.storage import save_multi_credentials

log = logging.getLogger("grazie2api.credentials")

router = APIRouter(tags=["credentials"])


# ---------------------------------------------------------------------------
# Smart credential parser — one-paste auto-detect
# ---------------------------------------------------------------------------

def _looks_like_jwt(s: str) -> bool:
    """Check if string looks like a JWT (3 base64url parts separated by dots)."""
    parts = s.split(".")
    return len(parts) == 3 and all(len(p) > 10 for p in parts)


def _looks_like_refresh_token(s: str) -> bool:
    """Refresh tokens are typically long opaque strings, 40+ chars."""
    return len(s) >= 40 and not _looks_like_jwt(s)


def _parse_credential_blob(blob: str) -> dict[str, Any]:
    """Auto-parse a credential blob — supports JSON, key=value, and raw paste.

    Accepted formats:
      1. JSON object: {"jwt": "...", "refresh_token": "...", "license_id": "..."}
      2. Key=value lines:
           jwt=eyJ...
           refresh_token=1//...
           license_id=ABC123
      3. Raw paste: just paste tokens separated by whitespace/newlines — we detect which is which
      4. Single JWT: auto-detect and accept

    Returns parsed dict with keys: jwt, refresh_token, license_id, id_token, raw_input
    """
    blob = blob.strip()
    result: dict[str, Any] = {"raw_input_len": len(blob)}

    # Try JSON first
    if blob.startswith("{"):
        try:
            data = json.loads(blob)
            if isinstance(data, dict):
                result["jwt"] = data.get("jwt") or data.get("token") or data.get("grazie_jwt") or ""
                result["refresh_token"] = data.get("refresh_token") or data.get("rt") or ""
                result["license_id"] = data.get("license_id") or data.get("licenseId") or ""
                result["id_token"] = data.get("id_token") or ""
                result["user_email"] = data.get("user_email") or data.get("email") or ""
                result["label"] = data.get("label") or data.get("name") or ""
                return result
        except json.JSONDecodeError:
            pass

    # Try key=value or "Key: value" format (supports multi-word keys like "License ID")
    kv_pattern = re.compile(r'^([\w\s]+?)\s*[=:]\s*(.+)', re.MULTILINE)
    kv_matches = kv_pattern.findall(blob)
    if len(kv_matches) >= 2:
        # Normalize keys: lowercase, replace spaces with underscore
        kv_map = {k.lower().strip().replace(" ", "_"): v.strip().strip('"').strip("'") for k, v in kv_matches}
        result["jwt"] = kv_map.get("jwt") or kv_map.get("token") or kv_map.get("grazie_jwt") or ""
        result["refresh_token"] = kv_map.get("refresh_token") or kv_map.get("rt") or ""
        result["license_id"] = kv_map.get("license_id") or kv_map.get("licenseid") or ""
        result["id_token"] = kv_map.get("id_token") or ""
        result["user_email"] = kv_map.get("user_email") or kv_map.get("email") or ""
        result["label"] = kv_map.get("label") or kv_map.get("name") or ""
        # Also pick up API Key and API Base from portal format
        result["api_key"] = kv_map.get("api_key") or ""
        result["api_base"] = kv_map.get("api_base") or ""
        if any(result.get(k) for k in ("jwt", "refresh_token", "license_id")):
            return result

    # Raw paste: split by whitespace/newlines, detect token types
    tokens = blob.split()
    jwts: list[str] = []
    rts: list[str] = []
    short_ids: list[str] = []

    for t in tokens:
        t = t.strip().strip(",").strip('"').strip("'")
        if not t:
            continue
        if _looks_like_jwt(t):
            jwts.append(t)
        elif _looks_like_refresh_token(t):
            rts.append(t)
        elif 5 <= len(t) <= 20 and re.match(r'^[A-Z0-9]+$', t):
            short_ids.append(t)

    result["jwt"] = jwts[0] if jwts else ""
    # If multiple JWTs, second might be id_token
    result["id_token"] = jwts[1] if len(jwts) > 1 else ""
    result["refresh_token"] = rts[0] if rts else ""
    result["license_id"] = short_ids[0] if short_ids else ""

    return result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.post("/api/credentials/paste")
async def paste_credential(request: Request):
    """One-click credential paste — auto-parse any format.

    Accepts:
      - JSON body with "blob" field (raw text to parse)
      - JSON body with structured fields (jwt, refresh_token, license_id)
      - Plain text body (raw credential paste)

    Returns the parsed + stored credential info.
    """
    content_type = request.headers.get("content-type", "")
    blob = ""
    extra_fields: dict[str, str] = {}

    if "application/json" in content_type:
        try:
            body = await request.json()
        except Exception:
            raise HTTPException(400, "Invalid JSON")

        if isinstance(body, dict):
            blob = body.get("blob") or body.get("raw") or ""
            # If no blob, try structured fields directly
            if not blob and any(body.get(k) for k in ("jwt", "refresh_token", "license_id", "token")):
                blob = json.dumps(body)
            extra_fields = {
                "label": body.get("label") or "",
                "user_email": body.get("user_email") or body.get("email") or "",
            }
    else:
        blob = (await request.body()).decode("utf-8", errors="replace")

    if not blob.strip():
        raise HTTPException(400, "Empty input — paste your credentials (JWT, refresh_token, license_id)")

    parsed = _parse_credential_blob(blob)

    # Merge extra fields
    for k, v in extra_fields.items():
        if v and not parsed.get(k):
            parsed[k] = v

    jwt = parsed.get("jwt", "")
    refresh_token = parsed.get("refresh_token", "")
    license_id = parsed.get("license_id", "")
    id_token = parsed.get("id_token", "")

    if not jwt and not refresh_token:
        raise HTTPException(400, "Could not detect JWT or refresh_token from input")

    # Build credential entry
    cred_id = f"cred-{uuid.uuid4().hex[:8]}"
    cred_data = {
        "id": cred_id,
        "label": parsed.get("label") or parsed.get("user_email") or cred_id,
        "refresh_token": refresh_token,
        "license_id": license_id,
        "user_email": parsed.get("user_email", ""),
        "added_at": int(time.time()),
    }

    if id_token:
        cred_data["id_token"] = id_token

    # If we have a JWT and license_id, we can add to pool immediately
    if jwt and license_id:
        entry = CredentialEntry(cred_data, state.settings)
        entry.token_manager.jwt = jwt

        # Decode JWT to set expiry
        from src.auth.pkce import decode_jwt_payload
        claims = decode_jwt_payload(jwt)
        entry.token_manager.jwt_expires = claims.get("exp", 0)

        if id_token:
            entry.token_manager.id_token = id_token
            id_claims = decode_jwt_payload(id_token)
            entry.token_manager.id_token_expires = id_claims.get("exp", time.time() + 3600)

        if state.http_client:
            entry.attach_client(state.http_client)
        if state.pool:
            state.pool.add_entry(entry)
            _persist_pool()
            log.info("Credential %s added to pool (jwt+license_id ready)", cred_id)
    elif refresh_token and license_id:
        # Have RT + license_id but no JWT — can refresh on first use
        entry = CredentialEntry(cred_data, state.settings)
        if state.http_client:
            entry.attach_client(state.http_client)
        if state.pool:
            state.pool.add_entry(entry)
            _persist_pool()
            log.info("Credential %s added to pool (RT+license, JWT will refresh on first use)", cred_id)
    else:
        log.warning("Credential %s stored but incomplete (missing license_id or refresh_token)", cred_id)

    return JSONResponse({
        "ok": True,
        "credential_id": cred_id,
        "parsed": {
            "has_jwt": bool(jwt),
            "has_refresh_token": bool(refresh_token),
            "has_license_id": bool(license_id),
            "has_id_token": bool(id_token),
            "label": cred_data["label"],
        },
        "status": "ready" if (jwt and license_id) else "needs_refresh" if (refresh_token and license_id) else "incomplete",
        "hint": _status_hint(jwt, refresh_token, license_id),
    })


@router.post("/api/credentials/webhook")
async def webhook_add_credential(request: Request):
    """External activation service webhook — inject a fully activated credential.

    Expected JSON body:
    {
        "jwt": "eyJ...",
        "refresh_token": "1//...",
        "license_id": "ABC123",
        "id_token": "eyJ...",       // optional
        "user_email": "...",         // optional
        "label": "...",              // optional
        "webhook_secret": "..."      // optional auth
    }
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    jwt = body.get("jwt") or body.get("token") or ""
    refresh_token = body.get("refresh_token") or ""
    license_id = body.get("license_id") or body.get("licenseId") or ""

    if not refresh_token or not license_id:
        raise HTTPException(400, "webhook requires at minimum: refresh_token + license_id")

    cred_id = f"cred-{uuid.uuid4().hex[:8]}"
    cred_data = {
        "id": cred_id,
        "label": body.get("label") or body.get("user_email") or cred_id,
        "refresh_token": refresh_token,
        "license_id": license_id,
        "user_email": body.get("user_email", ""),
        "added_at": int(time.time()),
    }

    entry = CredentialEntry(cred_data, state.settings)

    if jwt:
        entry.token_manager.jwt = jwt
        from src.auth.pkce import decode_jwt_payload
        claims = decode_jwt_payload(jwt)
        entry.token_manager.jwt_expires = claims.get("exp", 0)

    id_token = body.get("id_token", "")
    if id_token:
        entry.token_manager.id_token = id_token
        from src.auth.pkce import decode_jwt_payload
        id_claims = decode_jwt_payload(id_token)
        entry.token_manager.id_token_expires = id_claims.get("exp", time.time() + 3600)

    if state.http_client:
        entry.attach_client(state.http_client)
    if state.pool:
        state.pool.add_entry(entry)
        _persist_pool()

    log.info("Webhook: credential %s added (email=%s)", cred_id, body.get("user_email", ""))
    return JSONResponse({"ok": True, "credential_id": cred_id, "status": "ready" if jwt else "needs_refresh"})


@router.get("/api/credentials")
async def list_credentials(request: Request):
    """List all credentials in the pool (admin-only if api_key is set)."""
    _require_admin(request)
    if not state.pool:
        return JSONResponse({"credentials": [], "count": 0})

    creds = []
    for entry in state.pool.entries():
        info = entry.to_dict()
        # Redact sensitive fields
        info.pop("refresh_token", None)
        creds.append(info)

    return JSONResponse({"credentials": creds, "count": len(creds)})


@router.delete("/api/credentials/{cred_id}")
async def remove_credential(cred_id: str, request: Request):
    """Remove a credential from the pool."""
    _require_admin(request)
    if not state.pool:
        raise HTTPException(404, "No pool configured")

    removed = state.pool.remove_entry(cred_id)
    if removed:
        _persist_pool()
        return JSONResponse({"ok": True, "removed": cred_id})
    raise HTTPException(404, f"Credential {cred_id} not found")


@router.get("/health")
async def health():
    """Health check endpoint."""
    pool_size = state.pool.count() if state.pool else 0
    available = state.pool.available_count() if state.pool else 0
    return JSONResponse({
        "status": "ok",
        "pool_size": pool_size,
        "available": available,
        "strategy": state.strategy,
    })


@router.get("/credentials", response_class=HTMLResponse)
async def credentials_page():
    """Simple credential management UI."""
    return HTMLResponse(_CREDENTIALS_HTML)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_admin(request: Request) -> None:
    """Check admin API key if configured."""
    if not state.api_key:
        return
    auth = request.headers.get("authorization", "")
    if auth.replace("Bearer ", "").strip() != state.api_key:
        raise HTTPException(401, "Unauthorized")


def _persist_pool() -> None:
    """Persist current pool credentials to disk."""
    if not state.pool:
        return
    creds_data = []
    for entry in state.pool.entries():
        creds_data.append({
            "id": entry.id,
            "label": entry.label,
            "refresh_token": entry.refresh_token,
            "license_id": entry.license_id,
            "user_email": entry.user_email,
            "added_at": entry.added_at,
        })
    save_multi_credentials(creds_data, state.settings)


def _status_hint(jwt: str, rt: str, lid: str) -> str:
    if jwt and lid:
        return "Credential is ready to use."
    if rt and lid:
        return "JWT will be obtained automatically on first API call."
    if rt and not lid:
        return "Missing license_id — use OAuth browser login or provide it manually."
    if jwt and not lid:
        return "JWT provided but no license_id for refresh — credential will expire and cannot be renewed."
    return "Incomplete credential — need at minimum: refresh_token + license_id, or a valid JWT."


# ---------------------------------------------------------------------------
# Embedded credential management page
# ---------------------------------------------------------------------------

_CREDENTIALS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>grazie2api - Credentials</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; padding: 20px; max-width: 800px; margin: 0 auto; }
  h1 { font-size: 1.5em; margin-bottom: 16px; color: #58a6ff; }
  .section { background: #161b22; border: 1px solid #30363d; border-radius: 8px; padding: 16px; margin-bottom: 16px; }
  .section h2 { font-size: 1.1em; margin-bottom: 12px; color: #8b949e; }
  textarea { width: 100%; height: 120px; background: #0d1117; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 10px; font-family: monospace; font-size: 13px; resize: vertical; }
  textarea::placeholder { color: #484f58; }
  button { background: #238636; color: #fff; border: none; border-radius: 6px; padding: 8px 16px; cursor: pointer; font-size: 14px; margin-top: 8px; }
  button:hover { background: #2ea043; }
  button.danger { background: #da3633; }
  button.danger:hover { background: #f85149; }
  .result { margin-top: 12px; padding: 10px; border-radius: 6px; font-family: monospace; font-size: 13px; white-space: pre-wrap; display: none; }
  .result.ok { background: #0d2818; border: 1px solid #238636; color: #3fb950; display: block; }
  .result.err { background: #2d1013; border: 1px solid #da3633; color: #f85149; display: block; }
  .cred-list { list-style: none; }
  .cred-item { padding: 10px; border-bottom: 1px solid #21262d; display: flex; justify-content: space-between; align-items: center; }
  .cred-item:last-child { border-bottom: none; }
  .cred-label { font-weight: 600; }
  .cred-meta { font-size: 12px; color: #8b949e; }
  .badge { display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: 600; }
  .badge.ready { background: #238636; color: #fff; }
  .badge.pending { background: #9e6a03; color: #fff; }
  .badge.incomplete { background: #da3633; color: #fff; }
  .formats { margin-top: 8px; font-size: 12px; color: #8b949e; }
  .formats code { background: #21262d; padding: 2px 6px; border-radius: 4px; }
</style>
</head>
<body>
<h1>grazie2api</h1>

<div class="section">
  <h2>Add Credential</h2>
  <p style="font-size:13px;color:#8b949e;margin-bottom:8px;">Paste your credential in any format — JSON, key=value, or raw tokens. Auto-detected.</p>
  <textarea id="blob" placeholder='{"jwt": "eyJ...", "refresh_token": "1//...", "license_id": "ABC123"}

Or key=value:
jwt=eyJ...
refresh_token=1//...
license_id=ABC123

Or just paste tokens separated by spaces/newlines.'></textarea>
  <button onclick="submitCred()">Add Credential</button>
  <div id="result" class="result"></div>
  <div class="formats">
    Accepted: <code>JSON</code> <code>key=value</code> <code>raw tokens</code> <code>single JWT</code>
  </div>
</div>

<div class="section">
  <h2>Pool Status</h2>
  <div id="pool-status">Loading...</div>
  <ul class="cred-list" id="cred-list"></ul>
  <button onclick="loadCreds()" style="background:#30363d;margin-top:8px;">Refresh</button>
</div>

<script>
const API_KEY = localStorage.getItem('grazie2api_key') || '';
function headers() {
  const h = {'Content-Type': 'application/json'};
  if (API_KEY) h['Authorization'] = 'Bearer ' + API_KEY;
  return h;
}

async function submitCred() {
  const blob = document.getElementById('blob').value.trim();
  const res = document.getElementById('result');
  if (!blob) { res.className = 'result err'; res.textContent = 'Empty input'; return; }
  try {
    const resp = await fetch('/api/credentials/paste', {method: 'POST', headers: headers(), body: JSON.stringify({blob})});
    const data = await resp.json();
    if (resp.ok) {
      res.className = 'result ok';
      res.textContent = data.hint + '\\nID: ' + data.credential_id + '\\nStatus: ' + data.status;
      document.getElementById('blob').value = '';
      loadCreds();
    } else {
      res.className = 'result err';
      res.textContent = data.detail || JSON.stringify(data);
    }
  } catch(e) { res.className = 'result err'; res.textContent = String(e); }
}

async function loadCreds() {
  try {
    const [healthResp, credsResp] = await Promise.all([
      fetch('/health'),
      fetch('/api/credentials', {headers: headers()})
    ]);
    const health = await healthResp.json();
    const creds = await credsResp.json();

    document.getElementById('pool-status').innerHTML =
      '<span class="badge ' + (health.available > 0 ? 'ready' : 'pending') + '">' +
      health.available + '/' + health.pool_size + ' available</span> ' +
      '<span class="cred-meta">strategy: ' + health.strategy + '</span>';

    const list = document.getElementById('cred-list');
    list.innerHTML = '';
    for (const c of (creds.credentials || [])) {
      const li = document.createElement('li');
      li.className = 'cred-item';
      const badgeCls = c.jwt_state === 'ready' ? 'ready' : c.available ? 'pending' : 'incomplete';
      li.innerHTML =
        '<div><span class="cred-label">' + (c.label || c.id) + '</span> ' +
        '<span class="badge ' + badgeCls + '">' + c.jwt_state + '</span>' +
        '<div class="cred-meta">' + (c.user_email || '') + ' | license: ' + (c.license_id || 'none') + '</div></div>' +
        '<button class="danger" onclick="removeCred(\\'' + c.id + '\\')">Remove</button>';
      list.appendChild(li);
    }
  } catch(e) { console.error(e); }
}

async function removeCred(id) {
  if (!confirm('Remove credential ' + id + '?')) return;
  await fetch('/api/credentials/' + id, {method: 'DELETE', headers: headers()});
  loadCreds();
}

loadCreds();
</script>
</body>
</html>
"""
