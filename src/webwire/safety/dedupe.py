"""Dedupe store — prevents repeated identical writes within a TTL window.

Per review Q3 hardening:
- Dedupe key is canonical semantic intent (actor + action + target + variant),
  not just (action, target). Inverse actions (like vs unlike) are distinct.
- TTL-based (default 1h), so re-liking after the window is allowed.
- In-memory hot cache, BUT hydrated from the recent journal window on boot,
  so a process restart during a loop doesn't erase the guard.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

__all__ = ["DedupeStore"]


class DedupeStore:
    """TTL-based dedupe with optional journal hydration on boot."""

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

    def hydrate_from_journal(self, journal_path: Path) -> int:
        """Hydrate the dedupe store from recent journal entries (review Q3).

        Reads the journal NDJSON, finds write records within the TTL window,
        and re-records their dedupe keys. Returns the count hydrated.
        """
        if not journal_path.exists():
            return 0
        now = time.time()
        cutoff = now - self._ttl
        hydrated = 0
        try:
            with journal_path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Only write records with a dedupe key, within the TTL window.
                    if rec.get("policy_decision") != "allowed":
                        continue
                    if rec.get("capability_tier") != "write":
                        continue
                    dk = rec.get("dedupe_key")
                    ts = rec.get("timestamp_epoch") or rec.get("duration_ms")
                    if not dk:
                        continue
                    # Journal records ISO timestamp; parse it for window check.
                    iso = rec.get("timestamp")
                    rec_time = _parse_iso(iso) if iso else None
                    if rec_time is None or rec_time < cutoff:
                        continue
                    self._entries[dk] = rec_time + self._ttl
                    hydrated += 1
        except OSError:
            pass
        return hydrated

    def _prune(self, now: float) -> None:
        """Remove expired entries."""
        expired = [k for k, exp in self._entries.items() if exp <= now]
        for k in expired:
            del self._entries[k]

    def size(self) -> int:
        return len(self._entries)


def _parse_iso(iso: str) -> Optional[float]:
    """Parse an ISO 8601 timestamp to epoch seconds. Returns None on failure."""
    try:
        # Handle the 'Z' suffix and fractional seconds.
        s = iso.replace("Z", "+00:00")
        from datetime import datetime
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None
