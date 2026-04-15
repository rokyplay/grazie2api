"""JSON file CRUD for credential storage."""

from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path

from src.config import Settings

log = logging.getLogger("grazie2api.storage")


def load_legacy_credentials(settings: Settings) -> dict:
    """Load credentials from legacy single-credential file."""
    path = settings.legacy_credentials_file
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to parse legacy credentials file %s: %s", path, e)
    return {}


def save_legacy_credentials(data: dict, settings: Settings) -> None:
    """Merge and save credentials to legacy file (atomic via tmp+replace)."""
    path = settings.legacy_credentials_file
    existing = load_legacy_credentials(settings)
    existing.update(data)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(existing, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)
    log.info("Credentials saved to %s", path)


def load_multi_credentials(settings: Settings) -> list[dict]:
    """Load the list of credentials from CONFIG_HOME/credentials.json."""
    path = settings.multi_credentials_file
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except Exception as e:
            log.warning("Failed to load %s: %s", path, e)
    return []


def save_multi_credentials(creds: list[dict], settings: Settings) -> None:
    """Persist the list of credentials atomically."""
    path = settings.multi_credentials_file
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(creds, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    try:
        tmp.chmod(0o600)
    except OSError:
        pass
    tmp.replace(path)


def migrate_legacy_credentials(settings: Settings) -> list[dict]:
    """If legacy single-credential file exists and multi is empty, migrate it."""
    multi = load_multi_credentials(settings)
    if multi:
        return multi

    legacy = load_legacy_credentials(settings)
    if not legacy or not legacy.get("refresh_token"):
        return multi

    cred_id = f"cred-{uuid.uuid4().hex[:8]}"
    label = legacy.get("user_email") or legacy.get("user_name") or cred_id
    entry = {
        "id": cred_id,
        "label": label,
        "refresh_token": legacy["refresh_token"],
        "license_id": legacy.get("license_id", ""),
        "added_at": legacy.get("obtained_at", int(time.time())),
        "user_email": legacy.get("user_email"),
        "user_name": legacy.get("user_name"),
    }
    multi = [entry]
    save_multi_credentials(multi, settings)
    log.info("Migrated legacy credential %s -> %s", label, settings.multi_credentials_file)
    return multi


def add_multi_credential(
    settings: Settings,
    refresh_token: str,
    license_id: str = "",
    label: str = "",
    user_email: str = "",
    user_name: str = "",
) -> dict:
    """Add a new credential to the multi-credential store.

    If refresh_token already exists, update it in place.
    """
    multi = load_multi_credentials(settings)
    for entry in multi:
        if entry.get("refresh_token") == refresh_token:
            entry["license_id"] = license_id or entry.get("license_id", "")
            entry["label"] = label or entry.get("label", "")
            entry["user_email"] = user_email or entry.get("user_email")
            entry["user_name"] = user_name or entry.get("user_name")
            save_multi_credentials(multi, settings)
            return entry

    cred_id = f"cred-{uuid.uuid4().hex[:8]}"
    entry = {
        "id": cred_id,
        "label": label or user_email or cred_id,
        "refresh_token": refresh_token,
        "license_id": license_id,
        "added_at": int(time.time()),
        "user_email": user_email,
        "user_name": user_name,
    }
    multi.append(entry)
    save_multi_credentials(multi, settings)
    return entry


def remove_multi_credential(cred_id: str, settings: Settings) -> bool:
    """Remove a credential by id. Returns True if removed."""
    multi = load_multi_credentials(settings)
    new_multi = [c for c in multi if c.get("id") != cred_id]
    if len(new_multi) == len(multi):
        return False
    save_multi_credentials(new_multi, settings)
    return True


def update_multi_refresh_token(cred_id: str, new_refresh_token: str, settings: Settings) -> None:
    """Persist an updated refresh_token for a credential id."""
    if not cred_id or not new_refresh_token:
        return
    multi = load_multi_credentials(settings)
    changed = False
    for entry in multi:
        if entry.get("id") == cred_id:
            if entry.get("refresh_token") != new_refresh_token:
                entry["refresh_token"] = new_refresh_token
                entry["refreshed_at"] = int(time.time())
                changed = True
            break
    if changed:
        save_multi_credentials(multi, settings)
