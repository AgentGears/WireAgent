"""Session manager — owns the Super-Browser lifecycle and session persistence.

The session manager is the ONLY component that holds the raw ``SuperBrowser``
instance. Per the Phase 0a design (Point 2 decision B):
- capability code never receives the raw facade;
- the dispatcher owns the ``sb`` instance via this manager;
- capabilities receive only the ``ReadOnlyBroker`` interface.

Default mode is LAUNCH — Super-Browser starts its OWN Chromium (owned browser).
Attach mode is a non-production diagnostic path, refused unless allow_attach=True
(review Q4 invariant: Agent-WebWire must own its browser by default).

Session persistence uses Super-Browser's public save_session/load_session
(cookie serialization, backend-agnostic). Per review Q1-Q3:
- load belongs early (before first X navigation); trusting belongs never;
- whoami is the real auth gate — persistence is a convenience, not authority;
- checkpoint eagerly after verified whoami; never save a logged-out jar over
  a known-good file; write atomically.
"""

from __future__ import annotations

import logging
from typing import Optional

from super_browser import Config as SBConfig
from super_browser import SuperBrowser
from super_browser.browser.config import SessionConfig, SessionMode

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, FailureCategory, hard_failure, ok_result

logger = logging.getLogger(__name__)

__all__ = ["SessionManager"]


class SessionManager:
    """Owns the Super-Browser lifecycle.

    Responsibilities (Phase 0a):
    - build a Super-Browser ``Config`` configured for attach mode;
    - ``start()`` / ``stop()`` with idempotent lifecycle;
    - expose the live ``SuperBrowser`` only to trusted internal callers
      (the dispatcher, which hands only the broker to capabilities).
    """

    def __init__(
        self,
        ww_config: Optional[WebWireConfig] = None,
        sb_config: Optional[SBConfig] = None,
    ) -> None:
        self._ww_config = ww_config or WebWireConfig()
        self._sb_config = sb_config or self._build_default_config()
        self._sb: Optional[SuperBrowser] = None
        self._started = False
        # Auth flag: True only after whoami has resolved identity in this run.
        # "Loading belongs early; trusting belongs never" — load_session sets
        # cookies but does NOT set this flag.
        self._authenticated = False
        # Actor identity (the whoami-resolved handle). Part of every write's
        # dedupe key, so multi-account writes never collide. None until whoami
        # succeeds this run — persistence never sets it (cookies are not identity).
        self._resolved_handle: Optional[str] = None
        self._session_loaded_state: str = "no_file"  # no_file|loaded|load_failed

    # -- accessors -----------------------------------------------------------

    @property
    def started(self) -> bool:
        return self._started

    @property
    def authenticated(self) -> bool:
        """True only after whoami resolved identity this run. Persistence is a
        convenience, not an authority source (review Q1/Q2 invariant)."""
        return self._authenticated

    @property
    def ownership(self) -> str:
        """Browser ownership mode: 'owned' (launched) or 'attached' (CDP)."""
        return "attached" if self._sb_config.browser.mode == SessionMode.PATCHRIGHT_ATTACH else "owned"

    @property
    def session_loaded_state(self) -> str:
        """How session restore went on start: no_file|loaded|load_failed."""
        return self._session_loaded_state

    def mark_authenticated(self) -> None:
        """Called by the dispatcher after whoami success. Enables checkpointing."""
        self._authenticated = True

    @property
    def resolved_handle(self) -> Optional[str]:
        """The whoami-resolved handle for this run, or None. This is the actor
        identity bound into write intents and dedupe keys."""
        return self._resolved_handle

    def set_resolved_handle(self, handle: Optional[str]) -> None:
        """Record the actor identity after a verified whoami. Empty/None is
        ignored (a failed or handle-less whoami must not clear a known-good
        identity mid-run)."""
        if handle and isinstance(handle, str) and handle.strip():
            self._resolved_handle = handle.strip()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> ActionResult:
        """Start Super-Browser and restore persisted session if present.

        Lifecycle invariant: load persisted cookies BEFORE first navigation,
        but do NOT mark authenticated until whoami resolves identity on a real
        X surface. ``load_session`` success means only "cookies were loaded."
        """
        if self._started:
            return ok_result(data={"already_started": True})

        # Attach gate (review Q4 invariant): refuse ambient CDP attachment
        # unless explicitly opted in. Prevents the bridge-browser mistake.
        if self._sb_config.browser.mode == SessionMode.PATCHRIGHT_ATTACH and not self._ww_config.allow_attach:
            return hard_failure(
                "Attach mode refused: allow_attach=False (default). Agent-WebWire must own its "
                "browser by default; attach is a non-production diagnostic path. Set "
                "WebWireConfig(allow_attach=True) only for dev/diagnostics.",
                failure_category=FailureCategory.SECURITY,
            )

        # For PATCHRIGHT_ATTACH, resolve the browser-level CDP WebSocket URL.
        if self._sb_config.browser.mode == SessionMode.PATCHRIGHT_ATTACH:
            resolved = self._resolve_browser_ws_url(self._ww_config.cdp_ws_url)
            if resolved is None:
                return hard_failure(
                    f"Could not resolve browser CDP WS URL from {self._ww_config.cdp_ws_url}. "
                    "Is Chrome running with --remote-debugging-port?",
                    failure_category=FailureCategory.BROWSER_CRASH,
                )
            from dataclasses import replace as _replace
            new_browser = _replace(self._sb_config.browser, cdp_ws_url=resolved)
            self._sb_config = _replace(self._sb_config, browser=new_browser)
            logger.info("Resolved browser CDP WS: %s", resolved)

        try:
            self._sb = SuperBrowser(config=self._sb_config)
            await self._sb.start()
            self._started = True

            # Restore persisted session cookies if present (early, before any
            # X navigation). Non-fatal if it fails — whoami is the real gate.
            self._restore_session()

            logger.info("SessionManager started (mode=%s, ownership=%s, session=%s)",
                        self._sb_config.browser.mode.value, self.ownership, self._session_loaded_state)
            return ok_result(data={
                "started": True, "mode": self._sb_config.browser.mode.value,
                "ownership": self.ownership, "session": self._session_loaded_state,
            })
        except Exception as exc:  # noqa: BLE001 — surface as envelope, don't raise
            logger.exception("SessionManager start failed")
            self._sb = None
            return hard_failure(
                f"Failed to start Super-Browser session: {exc!r}",
                failure_category=FailureCategory.BROWSER_CRASH,
            )

    async def checkpoint_session(self) -> ActionResult:
        """Atomically save the current cookie jar to session.json.

        Called by the dispatcher after a successful whoami (eager checkpoint)
        and on clean stop IF authenticated. Never overwrites a known-good file
        with an unauthenticated jar (review Q3 invariant).
        """
        if not self._started or self._sb is None:
            return ok_result(data={"skipped": "not_started"})
        if not self._authenticated:
            # Don't save a logged-out/challenged state over a prior good session.
            logger.info("checkpoint_session skipped: not authenticated")
            return ok_result(data={"skipped": "not_authenticated"})
        target = self._ww_config.session_path()
        try:
            # save_session writes directly; for atomicity, save to a temp file
            # then replace. Super-Browser's save_session takes a path.
            tmp = target.with_suffix(target.suffix + ".tmp")
            r = await self._sb.save_session(str(tmp))
            if not r.ok:
                return r
            # Atomic replace on POSIX; on Windows os.replace is atomic too.
            import os
            os.replace(tmp, target)
            # Restrictive permissions where practical (best-effort, Windows).
            try:
                import stat
                os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
            logger.info("Session checkpointed to %s", target)
            return ok_result(data={"checkpointed": str(target)})
        except Exception as exc:  # noqa: BLE001
            return hard_failure(f"checkpoint_session failed: {exc!r}")

    def _restore_session(self) -> None:
        """Load persisted cookies if the session file exists. Synchronous-ish:
        runs an async save_session via the running loop. Sets _session_loaded_state."""
        import asyncio
        target = self._ww_config.session_path()
        if not target.exists():
            self._session_loaded_state = "no_file"
            return
        if self._sb is None:
            self._session_loaded_state = "load_failed"
            return
        try:
            loop = asyncio.get_event_loop()
            r = loop.run_until_complete(self._sb.load_session(str(target))) if not loop.is_running() else None
            if r is None:
                # We're inside a running loop (the normal case) — schedule it.
                # load_session is async; we can't await here without making
                # _restore_session async. Instead, set a flag and let the
                # dispatcher drive the load after start returns. For simplicity
                # in 0a, fall back to marking pending and loading lazily.
                self._session_loaded_state = "pending"
                return
            self._session_loaded_state = "loaded" if r.ok else "load_failed"
        except Exception as exc:  # noqa: BLE001
            logger.warning("Session restore failed: %r", exc)
            self._session_loaded_state = "load_failed"

    async def restore_session_async(self) -> ActionResult:
        """Async session restore — call from the dispatcher after start, before
        first navigation. This is the real load path; _restore_session just
        detects the file and sets state to 'pending'."""
        if self._session_loaded_state != "pending":
            return ok_result(data={"state": self._session_loaded_state})
        if self._sb is None:
            self._session_loaded_state = "load_failed"
            return hard_failure("No sb to load session into")
        target = self._ww_config.session_path()
        try:
            r = await self._sb.load_session(str(target))
            self._session_loaded_state = "loaded" if r.ok else "load_failed"
            return r
        except Exception as exc:  # noqa: BLE001
            self._session_loaded_state = "load_failed"
            return hard_failure(f"load_session failed: {exc!r}")

    @staticmethod
    def _resolve_browser_ws_url(ws_or_http_url: str) -> Optional[str]:
        """Resolve the browser-level /devtools/browser/<id> WS URL.

        Patchright needs the full browser endpoint, not the bare host:port.
        We strip the URL to an http origin and GET /json/version, which returns
        webSocketDebuggerUrl. Falls back to the input unchanged on any error.
        """
        import json as _json
        import urllib.request
        # Derive http://host:port from ws://host:port or http://host:port.
        raw = ws_or_http_url
        if raw.startswith("ws://"):
            origin = "http://" + raw[len("ws://"):]
        elif raw.startswith("wss://"):
            origin = "https://" + raw[len("wss://"):]
        elif raw.startswith("http"):
            origin = raw
        else:
            return None
        # Strip any trailing path on the origin for the version query.
        origin = origin.split("/", 3)
        origin = "/".join(origin[:3])  # scheme://host:port
        try:
            with urllib.request.urlopen(f"{origin}/json/version", timeout=5) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
            ws = data.get("webSocketDebuggerUrl")
            return ws
        except Exception as exc:  # noqa: BLE001
            logger.warning("CDP /json/version resolution failed: %r", exc)
            return None

    async def stop(self) -> ActionResult:
        """Stop the session. Idempotent. Does NOT auto-stop on kill-switch trip
        (per Point 3 decision — leaves the browser intact for debugging).

        Saves session cookies before stopping IF authenticated, so a clean run
        refreshes the saved jar. Never saves an unauthenticated jar."""
        if not self._started or self._sb is None:
            self._started = False
            return ok_result(data={"already_stopped": True})
        # Checkpoint if we have a verified identity (review Q3).
        if self._authenticated:
            try:
                await self.checkpoint_session()
            except Exception as exc:  # noqa: BLE001
                logger.warning("checkpoint during stop failed: %r", exc)
        try:
            await self._sb.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error during SessionManager.stop(): %r", exc)
        finally:
            self._sb = None
            self._started = False
            self._authenticated = False
            self._resolved_handle = None
        return ok_result(data={"stopped": True})

    # -- accessors -----------------------------------------------------------

    @property
    def sb(self) -> Optional[SuperBrowser]:
        """The live Super-Browser facade. None if not started.

        Intended for trusted internal callers only (dispatcher, broker setup).
        """
        return self._sb


    # -- internals -----------------------------------------------------------

    def _build_default_config(self) -> SBConfig:
        """Build a Super-Browser Config.

        Default mode is LAUNCH — Super-Browser starts its OWN Patchright
        Chromium (owned browser, golden path). The browser uses an ephemeral
        profile by default; session persistence is via cookie serialization
        (save_session/load_session), NOT profile dirs — SessionConfig.user_data_dir
        is currently a stub on the Super-Browser side (launch calls
        chromium.launch(), not launch_persistent_context). Set
        session_mode_override="patchright_attach" + allow_attach=True to attach
        to a running debug Chrome instead (non-production diagnostic path).
        """
        mode_str = self._ww_config.session_mode_override
        if mode_str:
            mode = SessionMode(mode_str)
            session = SessionConfig(
                mode=mode,
                headless=False,  # attach is headed by definition
                cdp_ws_url=self._ww_config.cdp_ws_url,
            )
        else:
            # LAUNCH (default). user_data_dir is set but currently ignored by
            # Super-Browser's launch path (ephemeral profile). Retained for the
            # future persistent-context feature.
            mode = SessionMode.PATCHRIGHT_LAUNCH
            session = SessionConfig(
                mode=mode,
                headless=False,  # headed so you can log in / watch it work
                user_data_dir=str(self._ww_config.chrome_profile_path()),
            )

        # Build the top-level Config with the composed browser session and
        # disabled subsystems. SBConfig fields are documented in
        # super_browser.config.Config.
        config = SBConfig(browser=session)
        # Disable subsystems we don't need in 0a. These are dataclass fields
        # on nested config objects; guard with getattr in case of version drift.
        try:
            config.agent.enable_recovery = False
            config.agent.enable_budget = False
            config.agent.enable_security = False  # we run our own broker in 0a
        except AttributeError:
            logger.debug("SBConfig shape drift — could not disable some agent subsystems")
        return config
