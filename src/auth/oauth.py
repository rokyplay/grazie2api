"""Browser-based OAuth PKCE flow and license ID discovery."""

from __future__ import annotations

import html as _html_mod
import logging
import re
import threading
import time
import urllib.parse
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler

import httpx

from src.auth.pkce import generate_pkce, decode_jwt_payload
from src.config import Settings

log = logging.getLogger("grazie2api.oauth")


class _OAuthCallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the OAuth localhost callback."""

    auth_code: str | None = None
    returned_state: str | None = None
    error: str | None = None

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _OAuthCallbackHandler.auth_code = params["code"][0]
            _OAuthCallbackHandler.returned_state = params.get("state", [""])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                b"<h1>Authorization Successful!</h1>"
                b"<p>You can close this tab and return to the terminal.</p>"
                b"</body></html>"
            )
        elif "error" in params:
            _OAuthCallbackHandler.error = params.get("error_description", params["error"])[0]
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            err_msg = _html_mod.escape(_OAuthCallbackHandler.error or "")
            self.wfile.write(
                f"<html><body style='font-family:sans-serif;text-align:center;padding:60px'>"
                f"<h1>Authorization Failed</h1><p>{err_msg}</p>"
                f"</body></html>".encode("utf-8")
            )
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def _find_callback_port(settings: Settings) -> int:
    """Find an available port for the OAuth callback server."""
    import socket
    start = settings.credentials.callback_port_start
    end = settings.credentials.callback_port_end
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No available port in range {start}-{end}")


def _redact_log(text: str) -> str:
    """Redact token-like patterns from log text."""
    import re
    return re.sub(r'(Bearer\s+)[A-Za-z0-9\-_\.]{20,}', r'\1<REDACTED>', text)


def oauth_login(settings: Settings) -> dict | None:
    """Run the full OAuth PKCE flow with browser login.

    Returns dict with access_token, refresh_token, id_token, etc. or None on failure.
    """
    _OAuthCallbackHandler.auth_code = None
    _OAuthCallbackHandler.returned_state = None
    _OAuthCallbackHandler.error = None

    code_verifier, code_challenge, state = generate_pkce()
    log.info("Generated PKCE parameters (state=%s)", state[:8])

    callback_port = _find_callback_port(settings)
    redirect_uri = f"http://127.0.0.1:{callback_port}/callback"
    log.info("Callback server on %s", redirect_uri)

    server = HTTPServer(("127.0.0.1", callback_port), _OAuthCallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    try:
        auth_url = (
            f"{settings.urls.hub_base}/api/rest/oauth2/auth"
            f"?client_id={settings.auth.client_id}"
            f"&response_type=code"
            f"&redirect_uri={urllib.parse.quote(redirect_uri, safe='')}"
            f"&scope={urllib.parse.quote(settings.auth.scope)}"
            f"&state={state}"
            f"&code_challenge_method={settings.auth.code_challenge_method}"
            f"&code_challenge={code_challenge}"
        )

        log.info("Opening browser for JetBrains login...")
        print("\n" + "=" * 60)
        print("  Please log in with your JetBrains account in the browser.")
        print("  If the browser doesn't open, visit this URL manually:")
        print(f"  {auth_url}")
        print("=" * 60 + "\n")

        webbrowser.open(auth_url)

        deadline = time.time() + 300
        while time.time() < deadline:
            if _OAuthCallbackHandler.auth_code or _OAuthCallbackHandler.error:
                break
            time.sleep(0.5)

        if _OAuthCallbackHandler.error:
            log.error("OAuth error: %s", _OAuthCallbackHandler.error)
            return None

        if not _OAuthCallbackHandler.auth_code:
            log.error("OAuth timeout: no callback received within 5 minutes")
            return None

        auth_code = _OAuthCallbackHandler.auth_code
        returned_state = _OAuthCallbackHandler.returned_state

        if returned_state != state:
            log.error(
                "OAuth state mismatch (expected=%s got=%s); aborting to prevent CSRF",
                state[:8], (returned_state or "")[:8],
            )
            return None

        log.info("Authorization code received")

        log.info("Exchanging code for tokens...")
        resp = httpx.post(
            settings.urls.hub_token_url,
            data={
                "grant_type": "authorization_code",
                "code": auth_code,
                "code_verifier": code_verifier,
                "client_id": settings.auth.client_id,
                "redirect_uri": redirect_uri,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            timeout=30,
            trust_env=False,
        )

        if resp.status_code != 200:
            log.error("Token exchange failed: %d %s", resp.status_code, _redact_log(resp.text[:300]))
            return None

        tokens = resp.json()
        access_token = tokens.get("access_token", "")
        refresh_token = tokens.get("refresh_token", "")
        id_token = tokens.get("id_token", "")
        expires_in = tokens.get("expires_in", 3600)

        if not refresh_token:
            log.error("No refresh_token in response!")
            return None

        id_claims = decode_jwt_payload(id_token) if id_token else {}

        log.info("Tokens obtained successfully!")
        log.info("  Name: %s", id_claims.get("name", "unknown"))
        log.info("  Email: %s", id_claims.get("email", "unknown"))
        log.info("  Token expires in: %ds", expires_in)

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": id_token,
            "expires_in": expires_in,
            "obtained_at": int(time.time()),
            "user_name": id_claims.get("name"),
            "user_email": id_claims.get("email"),
        }

    finally:
        server.shutdown()


def api_login(email: str, password: str, settings: Settings) -> dict | None:
    """Login via pure API (no browser) using email + password + PKCE.

    Returns dict with refresh_token, id_token, user_email etc. or None on failure.
    """
    client = httpx.Client(timeout=30, verify=True, follow_redirects=False, trust_env=False)

    try:
        # Step 1: Get CSRF token
        log.info("Logging in to JetBrains account (API mode)...")
        r = client.get(f"{settings.urls.jb_base}/login")
        xsrf = None
        for cookie in client.cookies.jar:
            if "_st" in cookie.name.lower():
                xsrf = cookie.value
                break

        headers = {"Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest"}
        if xsrf:
            headers["X-XSRF-TOKEN"] = xsrf

        # Step 2: Create auth session
        r = client.post(f"{settings.urls.jb_base}/api/auth/sessions", headers=headers, json={})
        if r.status_code != 200:
            log.error("Failed to create session: %d", r.status_code)
            return None
        sid = r.json().get("id", "")

        # Step 3: Email + password
        r = client.post(f"{settings.urls.jb_base}/api/auth/sessions/{sid}/email/login",
                        headers=headers, json={"email": email})
        state = r.json().get("state", "")
        if state != "PASSWORD_REQUIRED":
            log.error("Unexpected state after email: %s", state)
            return None

        r = client.post(f"{settings.urls.jb_base}/api/auth/sessions/{sid}/password",
                        headers=headers, json={"password": password})
        state = r.json().get("state", "")
        if state != "REDIRECT_TO_RETURN_URL":
            log.error("Login failed: state=%s (wrong password or 2FA required?)", state)
            return None
        log.info("JBA login successful")

        # Step 4: Check if card is bound (AI subscription check)
        log.info("Checking AI subscription status...")
        me_resp = client.get(f"{settings.urls.jb_base}/api/ai/account/settings",
                             headers={"Accept": "application/json"})
        if me_resp.status_code == 200:
            ai_settings = me_resp.json()
            show_plans = ai_settings.get("showAIPlans", True)
            if show_plans:
                log.warning("Account has NO active AI subscription (showAIPlans=True)")
                log.warning("Card binding and trial activation may be required")
                print("\n  WARNING: This account does not have an active AI subscription.")
                print("  You may need to bind a card and activate the AI trial first.")
            else:
                log.info("AI subscription active (showAIPlans=False)")

        # Step 5: PKCE + OAuth
        code_verifier, code_challenge, pkce_state = generate_pkce()

        params = {
            "client_id": settings.auth.client_id,
            "scope": settings.auth.scope,
            "code_challenge": code_challenge,
            "code_challenge_method": settings.auth.code_challenge_method,
            "state": pkce_state,
            "redirect_uri": f"{settings.urls.jb_base}/oauth2/ide/callback",
            "response_type": "code",
            "client_info": settings.auth.client_info,
        }
        url = f"{settings.urls.jb_base}/oauth/login?" + urllib.parse.urlencode(params)

        # Follow redirect chain to get final auth code
        final_code = None
        for i in range(15):
            r = client.get(url, follow_redirects=False)
            loc = r.headers.get("location", "")
            if "oauth2/ide/callback" in loc and "code=" in loc:
                parsed = urllib.parse.urlparse(loc)
                qs = urllib.parse.parse_qs(parsed.query)
                final_code = qs.get("code", [""])[0]
                log.info("OAuth authorization code obtained")
                break
            if not loc:
                log.error("OAuth redirect chain broken at step %d", i)
                break
            if loc.startswith("/"):
                parsed_url = urllib.parse.urlparse(str(r.url))
                url = f"{parsed_url.scheme}://{parsed_url.netloc}{loc}"
            else:
                url = loc

        if not final_code:
            log.error("Failed to obtain OAuth authorization code")
            return None

        # Step 6: Exchange code for tokens
        r = client.post(settings.urls.hub_token_url, data={
            "grant_type": "authorization_code",
            "code": final_code,
            "code_verifier": code_verifier,
            "client_id": settings.auth.client_id,
            "redirect_uri": f"{settings.urls.jb_base}/oauth2/ide/callback",
        })
        if r.status_code != 200:
            log.error("Token exchange failed: %d %s", r.status_code, r.text[:200])
            return None

        tokens = r.json()
        refresh_token = tokens.get("refresh_token", "")
        id_token = tokens.get("id_token", "")

        if not refresh_token:
            log.error("No refresh_token in response")
            return None

        id_claims = decode_jwt_payload(id_token) if id_token else {}
        log.info("Tokens obtained: email=%s name=%s", id_claims.get("email", "?"), id_claims.get("name", "?"))

        # Extract JBA session cookies for license discovery
        jba_cookies = {}
        for cookie in client.cookies.jar:
            jba_cookies[cookie.name] = cookie.value

        return {
            "refresh_token": refresh_token,
            "id_token": id_token,
            "access_token": tokens.get("access_token", ""),
            "expires_in": tokens.get("expires_in", 3600),
            "obtained_at": int(time.time()),
            "user_name": id_claims.get("name"),
            "user_email": id_claims.get("email") or email,
            "_jba_cookies": jba_cookies,
        }

    except Exception as e:
        log.error("API login failed: %s", e)
        return None
    finally:
        client.close()


_LICENSE_ID_CANDIDATES = ["AI", "FREE", "TRIAL", "PERSONAL", "community"]


def _extract_license_ids_from_page(jba_cookies: dict, settings: Settings) -> list[str]:
    """Fetch /licenses page using JBA session cookies and extract licenseId values.

    This is the most reliable method - directly scrapes the account page.
    Regex copied from verified script jb-parse-licenses.py.
    """
    if not jba_cookies:
        return []

    try:
        client = httpx.Client(timeout=30, verify=True, trust_env=False)
        for name, value in jba_cookies.items():
            client.cookies.set(name, value, domain="account.jetbrains.com")

        resp = client.get(f"{settings.urls.jb_base}/licenses")
        client.close()

        if resp.status_code != 200:
            log.warning("Failed to fetch /licenses page: %d", resp.status_code)
            return []

        # Extract license IDs from the /licenses HTML page
        ids = re.findall(r'id="license-([A-Z0-9]+)"', resp.text)
        log.info("Extracted %d licenseId(s) from /licenses page: %s", len(ids), ids)
        return ids

    except Exception as e:
        log.warning("Failed to extract licenseIds from /licenses page: %s", e)
        return []


def discover_license_id(id_token: str, settings: Settings, jba_cookies: dict | None = None) -> str | None:
    """Try to discover the licenseId.

    Strategy:
      1. Extract real licenseIds from /licenses page (most reliable, needs JBA cookies)
      2. Try each extracted id with provide-access to find the one that returns a JWT
      3. Fallback to hardcoded candidates if page extraction fails
    """
    headers = {
        "User-Agent": "ktor-client",
        "Content-Type": "application/json",
        "Accept-Charset": "UTF-8",
        "Authorization": f"Bearer {id_token}",
    }

    # Step 0: Register with JetBrains AI (idempotent, safe to call if already registered)
    try:
        reg_resp = httpx.post(
            settings.urls.register_url,
            headers={"Authorization": f"Bearer {id_token}", "User-Agent": "ktor-client"},
            timeout=15,
            trust_env=False,
        )
        log.info("AI register: %d %s", reg_resp.status_code, reg_resp.text[:200] if reg_resp.text else "")
    except Exception as e:
        log.warning("AI register call failed (non-fatal): %s", e)

    # Step 1: Try real licenseIds from /licenses page first
    page_ids = _extract_license_ids_from_page(jba_cookies or {}, settings)

    # Step 2: Build candidate list - page IDs first, then fallback candidates
    id_claims = decode_jwt_payload(id_token)
    candidates = list(page_ids)  # Real IDs first

    # Add fallback candidates (deduped)
    jba_id = id_claims.get("jba_account_id", "")
    if jba_id and str(jba_id) not in candidates:
        candidates.append(str(jba_id))
    for c in _LICENSE_ID_CANDIDATES:
        if c not in candidates:
            candidates.append(c)

    log.info("Trying %d licenseId candidates: %s", len(candidates), candidates)

    for lid in candidates:
        try:
            resp = httpx.post(
                settings.urls.jwt_url,
                json={"licenseId": lid},
                headers=headers,
                timeout=15,
                trust_env=False,
            )
            if resp.status_code == 200:
                data = resp.json()
                resp_state = data.get("state", "")
                token = data.get("token")
                log.info("  licenseId=%s -> state=%s token=%s", lid, resp_state, "YES" if token else "NO")
                if token:
                    log.info("Found working licenseId: %s (state=%s)", lid, resp_state)
                    return lid
            else:
                log.debug("  licenseId=%s -> %d", lid, resp.status_code)
        except Exception as e:
            log.debug("  licenseId=%s -> error: %s", lid, e)

    log.warning("Could not auto-discover licenseId from %d candidates.", len(candidates))
    return None
