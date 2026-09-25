"""Append-only invocation audit journal.

Layer 7 makes this file explicitly audit/diagnostic output only:

- NDJSON at ``.webwire/journal.ndjson``, one record per capability invocation
  (not per low-level browser action).
- ``browser_actions`` is a nested list of per-action summaries.
- Screenshots default to FAILURE-ONLY because X pages can expose private account
  content; config still supports never / on_failure / always.
- WRITE-tier records retain useful action/risk/dedupe facts as evidence, but no
  live safety component may rebuild execution authority from those fields.
- append I/O is intentionally best-effort and never participates in M5 commit,
  replay, approval, dedupe, or rate-limit authority.

Rotation remains an audit-retention concern: at 10 MB or 31 days of age,
whichever comes first, the active file is renamed to
``journal-YYYY-MM.ndjson`` and a fresh file starts. Six rotated files are
retained. History is aged out in whole files; no file is edited in place.

Thread-safety: append is a single ``write()`` of one line under the supported
single-process, async, single-writer model.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from webwire.config import ScreenshotPolicy, WebWireConfig

logger = logging.getLogger(__name__)

__all__ = [
    "BrowserActionEntry",
    "JournalRecord",
    "Journal",
    "read_recent_write_records",
]


@dataclass
class BrowserActionEntry:
    """One low-level browser action inside a capability invocation."""

    action: str
    params_redacted: dict[str, Any] = field(default_factory=dict)
    result_category: Optional[str] = None
    duration_ms: float = 0.0


@dataclass
class JournalRecord:
    """One capability invocation, serialized as one NDJSON line.

    ``policy_decision`` is audit metadata, not commit authority. Historical
    callers may use coarse Dispatcher values such as ``allowed``/``denied``;
    Layer 7 deliberately forbids interpreting this field as a safety fact.
    """

    timestamp: str
    trace_id: str
    capability: str
    target: Optional[str] = None
    input_redacted: dict[str, Any] = field(default_factory=dict)
    kill_switch_tripped: bool = False
    policy_decision: str = "allowed"
    result_ok: Optional[bool] = None
    success_category: Optional[str] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None
    browser_actions: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    screenshot: Optional[str] = None
    # Write facts remain useful for diagnostics/forensics only. They do not
    # hydrate DedupeStore, TokenBucket, RecoveryGuard, or commit authority.
    capability_tier: Optional[str] = None
    action_type: Optional[str] = None
    risk_tier: Optional[str] = None
    dedupe_key: Optional[str] = None

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))


class Journal:
    """Append-only NDJSON audit journal with size/age rotation."""

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        *,
        rotate_max_bytes: int = 10_000_000,
        rotate_age_days: float = 31.0,
        retain_rotated: int = 6,
    ) -> None:
        self._config = config or WebWireConfig()
        self._path: Path = self._config.journal_path()
        self._rotate_max_bytes = rotate_max_bytes
        self._rotate_age_days = rotate_age_days
        self._retain_rotated = retain_rotated
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create journal dir %s: %r", self._path.parent, exc)
        self._ready = True

    def rotated_paths(self) -> list[Path]:
        """Rotated journal files in the state dir, oldest-first by name."""

        parent = self._path.parent
        if not parent.exists():
            return []
        prefix = self._path.stem + "-"
        return sorted(
            p
            for p in parent.glob(f"{prefix}*.ndjson")
            if p.name != self._path.name
        )

    def maybe_rotate(self) -> None:
        """Rotate audit history when the configured size/age threshold is met."""

        try:
            if not self._path.exists() or self._path.stat().st_size == 0:
                return
            size = self._path.stat().st_size
            age_days = (time.time() - self._path.stat().st_mtime) / 86400.0
            if size < self._rotate_max_bytes and age_days < self._rotate_age_days:
                return
            stamp = datetime.now(timezone.utc).strftime("%Y-%m")
            target = self._path.parent / f"{self._path.stem}-{stamp}.ndjson"
            n = 2
            while target.exists():
                target = self._path.parent / f"{self._path.stem}-{stamp}-{n}.ndjson"
                n += 1
            self._path.rename(target)
            logger.info(
                "Journal rotated: %s (%d bytes, %.1f days old)",
                target.name,
                size,
                age_days,
            )
            rotated = self.rotated_paths()
            for stale in rotated[: max(0, len(rotated) - self._retain_rotated)]:
                try:
                    stale.unlink()
                    logger.info("Rotated journal pruned: %s", stale.name)
                except OSError as exc:
                    logger.warning("Could not prune rotated journal %s: %r", stale, exc)
        except OSError as exc:
            logger.warning("Journal rotation check failed: %r", exc)

    def append(self, record: JournalRecord) -> None:
        """Append one best-effort audit record; never affect capability outcome."""

        self._ensure()
        self.maybe_rotate()
        try:
            with self._path.open("a", encoding="utf-8") as f:
                f.write(record.to_jsonl() + "\n")
        except OSError as exc:
            logger.warning("Journal append failed: %r", exc)

    def should_capture_screenshot(self, failed: bool) -> bool:
        """Apply the configured screenshot policy to a capability outcome."""

        policy = self._config.screenshots
        if policy == ScreenshotPolicy.NEVER:
            return False
        if policy == ScreenshotPolicy.ALWAYS:
            return True
        return failed

    def screenshot_relpath(self, trace_id: str) -> Path:
        """Relative path for a screenshot artifact."""

        return self._config.screenshot_dir() / f"{trace_id}.png"


def read_recent_write_records(
    journal_path: Path,
    cutoff_epoch: float,
) -> list[dict[str, Any]]:
    """Retired Layer-7 compatibility shim; always returns no safety records.

    Before M5 completed, Dispatcher used this helper to rebuild in-memory
    dedupe/rate-limit state from best-effort audit rows. That made the journal a
    live safety input while still failing open on missing/corrupt data. Layer 7
    intentionally retires that role: EffectLedger + RecoveryGuard own durable
    unresolved-effect safety, while dedupe and token buckets are process-local
    defense-in-depth controls.

    The arguments are retained temporarily so older callers fail *safe with
    respect to authority separation* rather than silently continuing to consume
    journal rows. Diagnostic tooling that needs journal history should read it
    as audit data and must not feed the result into mutation authority.
    """

    del journal_path, cutoff_epoch
    return []
