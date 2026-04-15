"""CLI commands: serve, login, list, remove, stats, add."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

from src.config import Settings, load_settings
from src.credentials.storage import (
    migrate_legacy_credentials,
    add_multi_credential,
    remove_multi_credential,
    load_legacy_credentials,
    save_legacy_credentials,
)
from src.credentials.pool import CredentialPool
from src.stats.recorder import StatsRecorder
from src.auth.oauth import oauth_login, api_login, discover_license_id

log = logging.getLogger("grazie2api.cli")


def cli_list(settings: Settings) -> None:
    multi = migrate_legacy_credentials(settings)
    if not multi:
        print("No credentials configured.")
        return
    print(f"{'ID':<14} {'LABEL':<30} {'LICENSE':<14} {'ADDED'}")
    print("-" * 80)
    for c in multi:
        added = c.get("added_at", 0)
        added_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(added)) if added else "-"
        print(f"{c.get('id','-'):<14} {(c.get('label') or '-')[:30]:<30} "
              f"{(c.get('license_id') or '-'):<14} {added_str}")


def cli_remove(cred_id: str, settings: Settings) -> int:
    if remove_multi_credential(cred_id, settings):
        print(f"Removed credential {cred_id}")
        return 0
    print(f"Credential {cred_id} not found")
    return 1


def cli_stats(hours: int, settings: Settings) -> None:
    db_path = settings.stats_db_file
    if not db_path.exists():
        print("No stats database yet (run the server first).")
        return
    stats = StatsRecorder(db_path)
    data = stats.aggregate(hours=hours)
    print(f"=== Stats (last {hours}h) ===")
    print(f"Total requests: {data['total']}")
    print(f"Success:        {data['success']} ({data['success_rate']*100:.1f}%)")
    print(f"Avg latency:    {data['avg_latency_ms']}ms")
    print(f"Input tokens:   {data['input_tokens']}")
    print(f"Output tokens:  {data['output_tokens']}")
    print("\nBy credential:")
    for c in data["by_credential"]:
        print(f"  {c['credential_id']:<14} req={c['requests']} ok={c['success']} "
              f"in={c['input_tokens']} out={c['output_tokens']}")
    print("\nBy model:")
    for m in data["by_model"]:
        print(f"  {m['model']:<40} {m['requests']}")
    if data["errors"]:
        print("\nErrors:")
        for e in data["errors"]:
            print(f"  {e['error_code']:<20} {e['count']}")


def cli_add_from_json(json_path: Path, label: str, license_id: str, settings: Settings) -> int:
    if not json_path.exists():
        print(f"File not found: {json_path}")
        return 1
    try:
        data = json.loads(json_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"Failed to parse {json_path}: {e}")
        return 1
    entries = data if isinstance(data, list) else [data]
    added = 0
    for d in entries:
        if not isinstance(d, dict):
            continue
        rt = d.get("refresh_token")
        if not rt:
            print(f"  skip: no refresh_token in entry")
            continue
        lid = license_id or d.get("licenseId") or d.get("license_id") or ""
        lbl = label or d.get("label") or d.get("user_email") or d.get("email") or ""
        entry = add_multi_credential(
            settings,
            refresh_token=rt,
            license_id=lid,
            label=lbl,
            user_email=d.get("user_email") or d.get("email") or "",
            user_name=d.get("user_name") or d.get("name") or "",
        )
        print(f"  added {entry['id']}: label={entry['label']} license={entry['license_id'] or '-'}")
        added += 1
    print(f"Added/updated {added} credential(s)")
    return 0 if added > 0 else 1


def cli_login(args: argparse.Namespace, settings: Settings) -> int:
    import getpass

    print("=" * 60)
    print("  grazie2api -- Login")
    print("=" * 60)

    email = input("\n  Email: ").strip()
    if not email:
        print("Email cannot be empty.")
        return 1
    password = getpass.getpass("  Password: ")
    if not password:
        print("Password cannot be empty.")
        return 1

    print()
    result = api_login(email, password, settings)

    if not result:
        print("\n  API login failed. Falling back to browser OAuth...")
        result = oauth_login(settings)

    if not result:
        print("\nLogin failed!")
        return 1

    refresh_token = result["refresh_token"]
    id_token = result.get("id_token", "")

    license_id = args.license_id or ""
    if not license_id:
        print("\nDiscovering licenseId...")
        jba_cookies = result.get("_jba_cookies") or {}
        license_id = discover_license_id(id_token, settings, jba_cookies=jba_cookies) or ""

    if not license_id:
        print("  licenseId not found (account may need AI trial activation).")

    entry = add_multi_credential(
        settings,
        refresh_token=refresh_token,
        license_id=license_id,
        label=result.get("user_email") or result.get("user_name") or email,
        user_email=result.get("user_email") or email,
        user_name=result.get("user_name") or "",
    )
    print(f"\n  Saved: {entry['id']} (license={entry['license_id'] or 'pending'})")
    print(f"  Email: {entry.get('user_email', '-')}")
    if license_id:
        print("  AI subscription: active")
    else:
        print("  AI subscription: not detected (may still work if previously activated)")
    return 0


def serve(args: argparse.Namespace, settings: Settings) -> None:
    from src.api.app import state, create_app

    if args.host not in ("127.0.0.1", "localhost", "::1") and not args.api_key:
        log.critical(
            "Refusing to bind %s without --api-key (admin and LLM APIs would be publicly accessible)",
            args.host,
        )
        sys.exit(1)

    state.settings = settings
    state.api_key = args.api_key or os.environ.get("GRAZIE_API_KEY", "") or os.environ.get("JB_PROXY_API_KEY", "")
    state.strategy = args.strategy

    print("=" * 60)
    print("  grazie2api (multi-credential)")
    print("=" * 60)

    multi = migrate_legacy_credentials(settings)

    # Auto-login accounts from config.yaml if not already in credentials
    if settings.accounts:
        existing_emails = {c.get("user_email", "").lower() for c in multi if c.get("user_email")}
        for acct in settings.accounts:
            acct_email = acct.get("email", "")
            acct_pass = acct.get("password", "")
            if not acct_email or not acct_pass:
                continue
            if acct_email.lower() in existing_emails:
                log.info("Account %s already has credentials, skipping", acct_email)
                continue
            log.info("Auto-login for %s...", acct_email)
            result = api_login(acct_email, acct_pass, settings)
            if result:
                id_token = result.get("id_token", "")
                jba_cookies = result.get("_jba_cookies") or {}
                license_id = acct.get("license_id") or ""
                if not license_id and id_token:
                    license_id = discover_license_id(id_token, settings, jba_cookies=jba_cookies) or ""
                entry = add_multi_credential(
                    settings,
                    refresh_token=result["refresh_token"],
                    license_id=license_id or "",
                    label=result.get("user_email") or acct_email,
                    user_email=result.get("user_email") or acct_email,
                    user_name=result.get("user_name") or "",
                )
                multi.append(entry)
                log.info("  -> saved %s (license=%s)", entry["id"], entry.get("license_id") or "pending")
            else:
                log.warning("  -> auto-login failed for %s", acct_email)
        # Reload after auto-login
        multi = migrate_legacy_credentials(settings)

    if not multi:
        print("\nNo credentials found. Run one of:")
        print("  python main.py login             # browser OAuth")
        print("  python main.py add --file path   # import refresh_token(s)")
        sys.exit(1)

    print(f"\nLoaded {len(multi)} credential(s):")
    for c in multi:
        print(f"  {c['id']:<14} {c.get('label','-'):<30} license={c.get('license_id','-') or '-'}")

    state.pool = CredentialPool(multi, settings)
    state.stats = StatsRecorder(settings.stats_db_file)

    print(f"\nStrategy:   {state.strategy}")
    print(f"Config dir: {settings.config_home}")
    print(f"Stats DB:   {settings.stats_db_file}")
    print(f"\nStarting server on http://{args.host}:{args.port}")
    print(f"\nEndpoints:")
    print(f"  POST http://{args.host}:{args.port}/v1/chat/completions")
    print(f"  POST http://{args.host}:{args.port}/v1/messages")
    print(f"  POST http://{args.host}:{args.port}/v1/responses")
    print(f"  GET  http://{args.host}:{args.port}/v1/models")
    print(f"  GET  http://{args.host}:{args.port}/api/credentials")
    print(f"  GET  http://{args.host}:{args.port}/health")
    print(f"  GET  http://{args.host}:{args.port}/credentials")
    if state.api_key:
        if len(state.api_key) > 8:
            print(f"\n  API Key: {state.api_key[:4]}...{state.api_key[-4:]} (len={len(state.api_key)})")
        else:
            print(f"\n  API Key: {'*' * len(state.api_key)} (len={len(state.api_key)})")
    else:
        print(f"\n  No API key set (open access). Use --api-key to protect.")
    print()

    application = create_app()

    # DNS rebinding protection
    from fastapi.middleware.trustedhost import TrustedHostMiddleware
    allowed = ["127.0.0.1", "localhost", "::1",
               f"127.0.0.1:{args.port}", f"localhost:{args.port}", f"::1:{args.port}"]
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        allowed.extend([args.host, f"{args.host}:{args.port}"])
    for th in getattr(args, "trusted_host", []):
        if th not in allowed:
            allowed.append(th)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed)

    import uvicorn
    uvicorn.run(application, host=args.host, port=args.port, log_level="info")
