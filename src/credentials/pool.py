"""CredentialPool: runtime pool with rotation strategies."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import httpx
from fastapi import HTTPException

from src.credentials.entry import CredentialEntry
from src.config import Settings

if TYPE_CHECKING:
    from src.stats.recorder import StatsRecorder

log = logging.getLogger("grazie2api.pool")


def _is_quota_exhausted(entry: CredentialEntry) -> bool:
    """Check if a credential's quota is known to be exhausted.

    Returns True only when we have definitive quota data showing zero available.
    If quota data is missing/unknown, returns False (benefit of the doubt).
    """
    q = entry.quota or {}
    # Check 'available' field first (most reliable)
    avail = q.get("available")
    if avail is not None:
        try:
            return float(avail) <= 0
        except (TypeError, ValueError):
            pass
    # Fallback: check current >= maximum
    cur = q.get("current")
    mx = q.get("maximum")
    if cur is not None and mx is not None:
        try:
            return float(cur) >= float(mx)
        except (TypeError, ValueError):
            pass
    return False


class CredentialPool:
    """Runtime pool of credentials."""

    def __init__(self, credentials: list[dict], settings: Settings) -> None:
        self._entries: list[CredentialEntry] = [CredentialEntry(c, settings) for c in credentials]
        self._by_id: dict[str, CredentialEntry] = {e.id: e for e in self._entries}
        self._rr_index = 0
        self._lock = asyncio.Lock()

    def attach_client(self, client: httpx.AsyncClient) -> None:
        for e in self._entries:
            e.attach_client(client)

    def all(self) -> list[CredentialEntry]:
        return list(self._entries)

    def get(self, cred_id: str) -> CredentialEntry | None:
        return self._by_id.get(cred_id)

    def available(self) -> list[CredentialEntry]:
        """Return credentials that are not in cooldown AND not quota-exhausted."""
        result = []
        for e in self._entries:
            if not e.is_available():
                continue
            if _is_quota_exhausted(e):
                log.info(
                    "[cred %s] skipping in available(): quota exhausted (avail=%s, cur=%s, max=%s)",
                    e.id,
                    (e.quota or {}).get("available"),
                    (e.quota or {}).get("current"),
                    (e.quota or {}).get("maximum"),
                )
                continue
            result.append(e)
        return result

    def count(self) -> int:
        return len(self._entries)

    def available_count(self) -> int:
        """Count of currently available (not cooling, not exhausted) credentials."""
        return len(self.available())

    def entries(self) -> list[CredentialEntry]:
        """Return all entries (alias for all())."""
        return list(self._entries)

    def add(self, data: dict, settings: Settings) -> CredentialEntry:
        entry = CredentialEntry(data, settings)
        self._entries.append(entry)
        self._by_id[entry.id] = entry
        return entry

    def add_entry(self, entry: CredentialEntry) -> None:
        """Add a pre-built CredentialEntry to the pool."""
        self._entries.append(entry)
        self._by_id[entry.id] = entry

    def remove_entry(self, cred_id: str) -> bool:
        """Remove a credential by ID. Alias for remove()."""
        return self.remove(cred_id)

    def remove(self, cred_id: str) -> bool:
        entry = self._by_id.pop(cred_id, None)
        if not entry:
            return False
        self._entries = [e for e in self._entries if e.id != cred_id]
        return True

    async def pick(self, strategy: str, stats: "StatsRecorder | None") -> CredentialEntry:
        """Pick a credential using the given strategy.

        Skips credentials that are in cooldown or have exhausted quota.
        If all credentials are exhausted, raises a clear 429 error.
        """
        async with self._lock:
            avail = self.available()

            if not avail:
                # Check WHY none are available
                all_entries = [e for e in self._entries if e.license_id]
                if not all_entries:
                    raise HTTPException(
                        status_code=503,
                        detail={"error": {"message": "No usable credentials (all missing license_id)", "type": "no_credentials"}},
                    )

                # Separate exhausted vs cooldown
                exhausted = [e for e in all_entries if _is_quota_exhausted(e)]
                cooling = [e for e in all_entries if not e.is_available() and not _is_quota_exhausted(e)]

                if exhausted and not cooling:
                    # All credentials have exhausted quota
                    log.warning(
                        "All %d credentials have exhausted quota",
                        len(exhausted),
                    )
                    raise HTTPException(
                        status_code=429,
                        detail={
                            "error": {
                                "message": f"All {len(exhausted)} credential(s) have exhausted their quota. Quota resets periodically.",
                                "type": "quota_exhausted",
                            }
                        },
                    )

                if cooling:
                    # Some are in cooldown, pick the one that comes out soonest
                    log.info(
                        "No available credentials: %d exhausted, %d cooling down. Using soonest cooldown.",
                        len(exhausted), len(cooling),
                    )
                    avail = sorted(cooling, key=lambda e: e.cooldown_until)
                else:
                    # Fallback: all have license but somehow none available
                    avail = sorted(all_entries, key=lambda e: e.cooldown_until)

            if strategy == "least_used" and stats is not None:
                usage = stats.today_usage_map()
                avail.sort(key=lambda e: usage.get(e.id, 0))
                return avail[0]

            if strategy == "most_quota":
                def quota_score(e: CredentialEntry) -> float:
                    q = e.quota or {}
                    avail_amt = q.get("available")
                    if avail_amt is not None:
                        try:
                            return -float(avail_amt)
                        except (TypeError, ValueError):
                            pass
                    cur = q.get("current")
                    if cur is None:
                        return -1
                    try:
                        return -float(cur)
                    except (TypeError, ValueError):
                        return -1
                avail.sort(key=quota_score)
                return avail[0]

            # round_robin (default)
            self._rr_index = (self._rr_index + 1) % len(avail)
            return avail[self._rr_index]
