"""Append-only journal — one record per capability invocation.

Per the Phase 0a design (Point 4 decision):
- NDJSON at ``.webwire/journal.ndjson``, one record per capability invocation
  (NOT per low-level browser action).
- ``browser_actions`` is a nested list of per-action summaries.
- Screenshots default to FAILURE-ONLY (not every run) — X pages expose private
  timeline/DM/account content, and failure-only keeps the journal from becoming
  a surveillance artifact. Config supports never / on_failure / always.
- Records forward-compatible with future writes (Phase 0b/3): fields like
  ``policy_decision``, ``kill_switch_tripped`` are already present.

Thread-safety: append is a single ``write()`` of one line under a file lock —
sufficient for the single-process, async, single-writer Phase 0a model.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from webwire.config import ScreenshotPolicy, WebWireConfig

logger = logging.getLogger(__name__)

__all__ = ["BrowserActionEntry", "JournalRecord", "Journal"]


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

    def to_jsonl(self) -> str:
        return json.dumps(asdict(self), default=str, separators=(",", ":"))


class Journal:
    """Append-only NDJSON journal.

    The journal does not decide screenshot policy on its own — the caller
    (dispatcher) passes ``screenshot_captured`` and the relative path. The
    journal just records the truth.
    """

    def __init__(self, config: Optional[WebWireConfig] = None) -> None:
        self._config = config or WebWireConfig()
        self._path: Path = self._config.journal_path()
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

    def append(self, record: JournalRecord) -> None:
        """Append one record. Never raises into the capability path — journal
        failures are logged, not propagated, so a journal issue cannot block
        legitimate capability execution."""
        self._ensure()
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
