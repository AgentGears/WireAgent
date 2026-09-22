"""Dedupe store — prevents repeated identical writes within a TTL window.

Per review Q3 hardening:
- Dedupe key is canonical semantic intent (actor + action + target + variant),
  not just (action, target). Inverse actions (like vs unlike) are distinct.
- TTL-based (default 1h), so re-liking after the window is allowed.
- In-memory hot cache, hydrated from the recent journal window on boot, so a
  process restart during a loop doesn't erase the guard.

P0 hydration fix (2026-09-22): hydration now reads the write-fact fields the
journal actually writes (``dedupe_key`` on kernel-recorded writes), via the
shared tail-scan reader in ``webwire.journal``. The previous implementation
expected fields the journal never wrote and silently hydrated 0 entries.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from webwire.journal import read_recent_write_records

logger = logging.getLogger(__name__)

__all__ = ["DedupeStore"]


class DedupeStore:
    """TTL-based dedupe with journal hydration on boot."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._ttl = ttl_seconds
        # key -> expiry timestamp
        self._entries: dict[str, float] = {}

    def check(self, key: str, now: Optional[float] = None) -> bool:
        """True if the key is NOT a duplicate (i.e. the action is allowed).
        Does NOT record — call record() after a successful write."""
        t = now if now is not None else time.time()
        self._prune(t)
        expiry = self._entries.get(key)
        if expiry is not None and expiry > t:
            return False  # duplicate within TTL
        return True

    def record(self, key: str, now: Optional[float] = None) -> None:
        """Record that a write with this key was executed."""
        t = now if now is not None else time.time()
        self._entries[key] = t + self._ttl

    # -- hydration ----------------------------------------------------------

    def hydrate_records(self, records: Iterable[dict[str, Any]], now: Optional[float] = None) -> int:
        """Rebuild entries from journal write records (each carrying ``_epoch``
        and, for kernel-recorded writes, ``dedupe_key``). Returns the count
        hydrated. Records without a dedupe_key (gate-denied attempts) are
        skipped — they created no semantic write."""
        t = now if now is not None else time.time()
        hydrated = 0
        for rec in records:
            key = rec.get("dedupe_key")
            epoch = rec.get("_epoch")
            if not key or epoch is None:
                continue
            expiry = float(epoch) + self._ttl
            if expiry <= t:
                continue  # already outside the TTL window
            self._entries[key] = expiry
            hydrated += 1
        if hydrated:
            logger.info("DedupeStore hydrated %d entries from journal", hydrated)
        return hydrated

    def hydrate_from_journal(self, journal_path: Path, now: Optional[float] = None) -> int:
        """Hydrate from the journal at ``journal_path`` (compat entry point;
        the dispatcher feeds both stores from one shared read)."""
        t = now if now is not None else time.time()
        records = read_recent_write_records(journal_path, t - self._ttl)
        return self.hydrate_records(records, now=t)

    def _prune(self, now: float) -> None:
        """Remove expired entries."""
        expired = [k for k, exp in self._entries.items() if exp <= now]
        for k in expired:
            del self._entries[k]

    def size(self) -> int:
        return len(self._entries)
