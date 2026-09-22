"""Append-only journal — one record per capability invocation.

Per the Phase 0a design (Point 4 decision):
- NDJSON at ``.webwire/journal.ndjson``, one record per capability invocation
  (NOT per low-level browser action).
- ``browser_actions`` is a nested list of per-action summaries.
- Screenshots default to FAILURE-ONLY (not every run) — X pages expose private
  timeline/DM/account content, and failure-only keeps the journal from becoming
  a surveillance artifact. Config supports never / on_failure / always.

Write facts (P0 hydration fix, 2026-09-22): every WRITE-tier record carries
``capability_tier``, ``action_type``, ``risk_tier``, and — when the kernel
actually recorded the write in the dedupe store — ``dedupe_key``. These fields
are the source both safety stores rebuild from on start (dedupe memory +
token-bucket budgets). The journal is therefore load-bearing for safety, not
merely evidence; it is still never rewritten, only rotated whole.

Rotation (spec decision 3): at 10 MB or 31 days of age, whichever comes first,
the active file is renamed to ``journal-YYYY-MM.ndjson`` and a fresh file
starts. Six rotated files are retained. History is aged out in whole files;
no file is ever edited in place.

Thread-safety: append is a single ``write()`` of one line under a file lock —
sufficient for the single-process, async, single-writer model.
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
    "parse_iso_epoch",
]


@dataclass
class BrowserActionEntry:
    """One low-level browser action inside a capability invocation."""
    action: str
    params_redacted: dict[str, Any] = field(default_factory=dict)
    result_category: Optional[str] = None   # SuccessCategory / "failure"
    duration_ms: float = 0.0


@dataclass
class JournalRecord:
    """One capability invocation, serialized as one NDJSON line."""
    timestamp: str                           # ISO 8601 UTC
    trace_id: str
    capability: str
    target: Optional[str] = None             # redacted target URL/identifier
    input_redacted: dict[str, Any] = field(default_factory=dict)
    kill_switch_tripped: bool = False
    policy_decision: str = "allowed"         # allowed | denied | unsupported | killed
    result_ok: Optional[bool] = None
    success_category: Optional[str] = None
    failure_category: Optional[str] = None
    error_message: Optional[str] = None
    browser_actions: list[dict[str, Any]] = field(default_factory=list)
    duration_ms: float = 0.0
    screenshot: Optional[str] = None         # relative path if captured
    # -- Write facts (P0 hydration fix). Present only on WRITE-tier records
    # that reached the kernel's policy stage. dedupe_key is set ONLY when the
    # kernel recorded the write (success, or uncertain submit with
    # public_side_effect=True) — gate-denied attempts journal no key.
    capability_tier: Optional[str] = None    # "write" | None (reads stay None)
    action_type: Optional[str] = None        # kernel action_type, e.g. "bookmark"
    risk_tier: Optional[str] = None          # e.g. "private_reversible"
    dedupe_key: Optional[str] = None         # semantic key, only if recorded

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))


def parse_iso_epoch(iso: str) -> Optional[float]:
    """Parse an ISO 8601 timestamp to epoch seconds. Returns None on failure."""
    try:
        s = iso.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.timestamp()
    except (ValueError, TypeError):
        return None


class Journal:
    """Append-only NDJSON journal with size/age rotation.

    The journal does not decide screenshot policy on its own — the caller
    (dispatcher) passes ``screenshot_captured`` and the relative path. The
    journal just records the truth.
    """

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
        # Ensure parent exists lazily on first append.
        self._ready = False

    def _ensure(self) -> None:
        if self._ready:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("Could not create journal dir %s: %r", self._path.parent, exc)
        self._ready = True

    # -- rotation -----------------------------------------------------------

    def rotated_paths(self) -> list[Path]:
        """Rotated journal files in the state dir, oldest-first by name."""
        parent = self._path.parent
        if not parent.exists():
            return []
        prefix = self._path.stem + "-"  # "journal-"
        return sorted(
            p for p in parent.glob(f"{prefix}*.ndjson")
            if p.name != self._path.name
        )

    def maybe_rotate(self) -> None:
        """Rotate when the active file exceeds the size or age threshold.

        Renames the active file to ``journal-YYYY-MM.ndjson`` (UTC month of
        rotation; numeric suffix on collision), prunes beyond ``retain_rotated``,
        never rewrites content. Never raises into the capability path.
        """
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
                target.name, size, age_days,
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

    # -- append -------------------------------------------------------------

    def append(self, record: JournalRecord) -> None:
        """Append one record. Never raises into the capability path — journal
        failures are logged, not propagated, so a journal issue cannot block
        legitimate capability execution."""
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
        # ON_FAILURE (default)
        return failed

    def screenshot_relpath(self, trace_id: str) -> Path:
        """Relative path for a screenshot artifact."""
        return self._config.screenshot_dir() / f"{trace_id}.png"


# ---------------------------------------------------------------------------
# Hydration reader — shared by DedupeStore and TokenBucket
# ---------------------------------------------------------------------------

def read_recent_write_records(journal_path: Path, cutoff_epoch: float) -> list[dict[str, Any]]:
    """Return recent WRITE-tier records the safety stores rebuild from.

    Tail-scans the active journal file newest-to-oldest and stops at the first
    record older than ``cutoff_epoch`` (records are chronological). If every
    record in the active file is newer than the cutoff — i.e. a rotation
    boundary falls inside the window — scanning continues into the newest
    rotated file. An unparsable line (torn tail after a crash) is logged as a
    WARNING naming the file and line, and skipped; the rest still hydrates.

    Semantics for both consumers:
    - Only ``capability_tier == "write"`` and ``policy_decision == "allowed"``
      records are returned (dispatcher-level "allowed" = reached the kernel's
      policy stage).
    - Each returned dict carries ``_epoch`` (parsed timestamp in epoch seconds).
    - Bucket replay counts gate-denied invocations too — marginally
      conservative (blocks more), never less protective than live accounting.
    """
    records: list[dict[str, Any]] = []

    def _scan(path: Path) -> bool:
        """Scan one file newest-first. Returns True if the file is fully inside
        the window (scan should continue into the next-older file)."""
        if not path.exists():
            return False
        file_records: list[dict[str, Any]] = []
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as exc:
            logger.warning("Could not read journal %s: %r", path, exc)
            return False
        for lineno in range(len(lines) - 1, -1, -1):
            line = lines[lineno].strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                logger.warning(
                    "Journal %s line %d is not valid JSON (torn tail after a crash?) — skipped",
                    path.name, lineno + 1,
                )
                continue
            epoch = parse_iso_epoch(rec.get("timestamp") or "")
            if epoch is None:
                continue  # unparseable timestamp: cannot place in the window
            if epoch < cutoff_epoch:
                # Oldest-than-cutoff record: window closed for this file.
                file_records.reverse()
                records.extend(file_records)
                return False
            if rec.get("capability_tier") == "write" and rec.get("policy_decision") == "allowed":
                rec["_epoch"] = epoch
                file_records.append(rec)
        # Every record in this file was inside the window.
        file_records.reverse()
        records.extend(file_records)
        return True

    active_inside_window = _scan(journal_path)
    if active_inside_window:
        # Rotation boundary falls inside the window — continue into the newest
        # rotated file. If the active file is missing entirely (rotation
        # happened, then a crash before the first append), scan rotated too.
        parent = journal_path.parent
        prefix = journal_path.stem + "-"
        rotated = sorted(
            (p for p in parent.glob(f"{prefix}*.ndjson") if p.name != journal_path.name)
        ) if parent.exists() else []
        if rotated:
            _scan(rotated[-1])
    return records
