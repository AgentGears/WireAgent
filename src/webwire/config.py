"""Central Agent-WebWire configuration.

Immutable dataclass composed by the caller. Phase 0a fields only — the write
kernel (token buckets, dedupe, risk registry, dry-run) lands in Phase 0b and
will extend this dataclass, not replace it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Optional


class ScreenshotPolicy(StrEnum):
    """When to capture screenshots into the journal."""
    NEVER = "never"
    ON_FAILURE = "on_failure"   # default — keeps the journal non-surveillance
    ALWAYS = "always"


@dataclass(frozen=True)
class WebWireConfig:
    """Immutable configuration for an Agent-WebWire runtime."""

    # -- Runtime state directory. Holds journal.ndjson, kill file, screenshots.
    # Default is ./.webwire relative to CWD.
    state_dir: Path = field(default_factory=lambda: Path(".webwire"))

    # -- Screenshots ----------------------------------------------------------
    screenshots: ScreenshotPolicy = ScreenshotPolicy.ON_FAILURE

    # -- Kill switch ----------------------------------------------------------
    # Hot file path (relative to state_dir). Existence => tripped.
    kill_file: str = "kill"
    # Optional env var checked at boot only (boot-time default, not the active
    # kill mechanism — the hot file + in-process flag are the active mechanisms
    # per the Phase 0a design decision).
    kill_env_var: Optional[str] = "WEBWIRE_KILL"

    # -- X surface ------------------------------------------------------------
    home_url: str = "https://x.com/home"
    # Same-surface navigation rule: allowed URL prefixes for the read-only broker.
    allowed_url_prefixes: tuple[str, ...] = (
        "https://x.com/",
        "https://twitter.com/",
    )

    # -- Loop bounds (read-only broker) --------------------------------------
    max_steps_per_capability: int = 50
    max_scrolls_per_capability: int = 20
    max_runtime_seconds_per_capability: float = 60.0

    # -- Super-Browser session ------------------------------------------------
    # How to obtain the browser. PATCHRIGHT_ATTACH uses the user's already-
    # logged-in Chrome — the whole point of this tool.
    # If None, defaults to PATCHRIGHT_ATTACH.
    session_mode_override: Optional[str] = None

    def kill_path(self) -> Path:
        """Absolute path to the kill hot file."""
        return self.state_dir / self.kill_file

    def journal_path(self) -> Path:
        """Absolute path to the append-only journal."""
        return self.state_dir / "journal.ndjson"

    def screenshot_dir(self) -> Path:
        """Directory for screenshot artifacts."""
        return self.state_dir / "screenshots"
