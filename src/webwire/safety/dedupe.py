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

import logging
import time
from pathlib import Path
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

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

    def hydrate_records(
        self,
        records: Iterable[dict[str, Any]],
        now: Optional[float] = None,
    ) -> int:
        """Compatibility-only manual hydration; not used by the live M5 runtime.

        Layer 7 removes every supported journal-to-safety-state path. This
        helper remains temporarily for callers/tests that already hold explicit
        record data, but callers must not treat audit-journal rows as M5
        authority. ``hydrate_from_journal`` below is intentionally retired.
        """

        t = now if now is not None else time.time()
        hydrated = 0
        for rec in records:
            key = rec.get("dedupe_key")
            epoch = rec.get("_epoch")
            if not key or epoch is None:
                continue
            expiry = float(epoch) + self._ttl
            if expiry <= t:
                continue
            self._entries[key] = expiry
            hydrated += 1
        if hydrated:
            logger.info("DedupeStore manually hydrated %d compatibility entries", hydrated)
        return hydrated

    def hydrate_from_journal(
        self,
        journal_path: Path,
        now: Optional[float] = None,
    ) -> int:
        """Retired compatibility entry point; journal audit data is not authority."""

        del journal_path, now
        return 0

    def _prune(self, now: float) -> None:
        expired = [key for key, expiry in self._entries.items() if expiry <= now]
        for key in expired:
            del self._entries[key]

    def size(self) -> int:
        return len(self._entries)
