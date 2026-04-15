"""Dynamic model profiles fetching and alias resolution."""

from __future__ import annotations

import logging
import time

import httpx

from src.config import Settings

log = logging.getLogger("grazie2api.models")

# Module-level cache
_cached_profiles: list[str] = []
_profiles_fetched_at: float = 0


async def fetch_profiles(client: httpx.AsyncClient, jwt: str, settings: Settings) -> list[str]:
    """Fetch available model profiles from Grazie API."""
    global _cached_profiles, _profiles_fetched_at

    now = time.time()
    ttl = settings.models.profiles_cache_ttl_seconds
    if _cached_profiles and (now - _profiles_fetched_at) < ttl:
        return _cached_profiles

    exclude_keywords = set(settings.models.profile_exclude_keywords)
    fallback = settings.models.fallback_profiles

    try:
        resp = await client.get(
            settings.urls.profiles_url,
            headers={
                "grazie-authenticate-jwt": jwt,
                "grazie-agent": settings.grazie.agent_json,
                "User-Agent": "ktor-client",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        profiles: list[str] = []
        for p in data.get("profiles", []):
            pid = p.get("id", "")
            if any(kw in pid.lower() for kw in exclude_keywords):
                continue
            if pid:
                profiles.append(pid)
        if profiles:
            _cached_profiles = profiles
            _profiles_fetched_at = now
            log.info("Fetched %d model profiles from Grazie API", len(profiles))
            return profiles
        log.warning("Grazie API returned empty profiles, using fallback")
        return _cached_profiles or fallback
    except Exception as e:
        log.error("Failed to fetch profiles: %s", e)
        return _cached_profiles or fallback


def get_cached_profiles(settings: Settings) -> list[str]:
    """Return cached profiles or fallback (synchronous)."""
    return _cached_profiles or settings.models.fallback_profiles


def resolve_model(name: str, settings: Settings) -> str:
    """Resolve a model name (alias override or pass-through) to the Grazie profile name.

    Resolution order:
      1. User-defined alias_overrides in config.yaml
      2. Case-insensitive check of alias_overrides
      3. Pass-through (the name itself is used as the profile name)
    """
    overrides = settings.models.alias_overrides

    # Exact match
    resolved = overrides.get(name)
    if resolved:
        return resolved

    # Case-insensitive
    lower = name.lower()
    for alias, profile in overrides.items():
        if alias.lower() == lower:
            return profile

    return name
