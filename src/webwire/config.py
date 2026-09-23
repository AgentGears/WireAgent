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
    # How to obtain the browser. Default is LAUNCH — Super-Browser starts its
    # OWN Patchright Chromium. This is the golden path: a stealth browser
    # Agent-WebWire owns, isolated from other automation clients.
    #
    # Browser ownership is foundational (review Q4 invariant): attach mode is a
    # non-production diagnostic path and must be explicitly opted into.
    session_mode_override: Optional[str] = None  # None => LAUNCH (default)
    # Attach mode requires explicit opt-in. Refused by default to prevent the
    # "ambient CDP attachment" mistake (attaching to a foreign browser like the
    # ChatGPT-Web2API bridge's Chrome). Set True only for diagnostics/dev.
    allow_attach: bool = False
    # Dedicated Chrome profile directory for launch mode (currently a stub on
    # the Super-Browser side — launch uses an ephemeral profile). Retained for
    # the future persistent-context feature; ignored by the current launch path.
    chrome_profile_dir: str = "chrome-profile"
    # CDP WebSocket URL — only used when session_mode_override="patchright_attach".
    cdp_ws_url: str = "ws://127.0.0.1:9222"
    # Session-persistence file (cookie serialization via Super-Browser's public
    # save_session/load_session). Loaded on start if present; checkpointed after
    # verified whoami. Treated as a secret: gitignored, restrictive perms.
    session_file: str = "session.json"

    def kill_path(self) -> Path:
        """Absolute path to the kill hot file."""
        return self.state_dir / self.kill_file

    def journal_path(self) -> Path:
        """Absolute path to the append-only audit journal."""
        return self.state_dir / "journal.ndjson"

    def effects_path(self) -> Path:
        """Absolute path to the fsync-backed M5 safety ledger."""
        return self.state_dir / "effects.ndjson"

    def screenshot_dir(self) -> Path:
        """Directory for screenshot artifacts."""
        return self.state_dir / "screenshots"

    def chrome_profile_path(self) -> Path:
        """Absolute path to the dedicated Chrome profile (launch mode)."""
        return self.state_dir / self.chrome_profile_dir

    def session_path(self) -> Path:
        """Absolute path to the session-persistence file (cookie jar)."""
        return self.state_dir / self.session_file
