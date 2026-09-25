"""Process-local semantic dedupe for write invocations.

The key is canonical semantic intent (actor + action + target + variant), not
just ``(action, target)``. Inverse actions such as like/unlike remain distinct.

M5 Layer 7 deliberately makes this store process-local defense-in-depth. It is
not restart authority and is no longer rebuilt from ``journal.ndjson``. Durable
uncertain-effect replay safety belongs to EffectLedger + RecoveryGuard. A future
cross-restart dedupe requirement needs a dedicated persistence contract rather
than best-effort audit data.
"""

from __future__ import annotations

import time
from typing import Optional

__all__ = ["DedupeStore"]


class DedupeStore:
    """TTL-based in-memory semantic dedupe for the current process."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[str, float] = {}

    def check(self, key: str, now: Optional[float] = None) -> bool:
        """True when ``key`` is not a duplicate inside the live TTL window."""

        t = now if now is not None else time.time()
        self._prune(t)
        expiry = self._entries.get(key)
        if expiry is not None and expiry > t:
            return False
        return True

    def record(self, key: str, now: Optional[float] = None) -> None:
        """Record one semantic write in the current process-local window."""

        t = now if now is not None else time.time()
        self._entries[key] = t + self._ttl

    def _prune(self, now: float) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            del self._entries[key]

    def size(self) -> int:
        return len(self._entries)
