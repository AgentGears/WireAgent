"""Session manager — owns the Super-Browser lifecycle.

The session manager is the ONLY component that holds the raw ``SuperBrowser``
instance. Per the Phase 0a design (Point 2 decision B):
- capability code never receives the raw facade;
- the dispatcher owns the ``sb`` instance via this manager;
- capabilities receive only the ``ReadOnlyBroker`` interface.

Phase 0a uses ``SessionMode.PATCHRIGHT_ATTACH`` — attach to the user's already-
logged-in Chrome. That is the whole point of a browser-first tool on a personal
account: no credential handling, no anti-bot bypass, just the real session.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

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

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> ActionResult:
        """Start Super-Browser in attach mode.

        Returns an ``ActionResult`` so the caller can route failures through
        the same envelope as capabilities. On success, ``self.sb`` is live.
        """
        if self._started:
            return ok_result(data={"already_started": True})

        try:
            self._sb = SuperBrowser(config=self._sb_config)
            await self._sb.start()
            self._started = True
            logger.info("SessionManager started (mode=%s)", self._sb_config.browser.mode.value)
            return ok_result(data={"started": True, "mode": self._sb_config.browser.mode.value})
        except Exception as exc:  # noqa: BLE001 — surface as envelope, don't raise
            logger.exception("SessionManager start failed")
            # Do not retain a half-built instance.
            self._sb = None
            return hard_failure(
                f"Failed to start Super-Browser session: {exc!r}",
                failure_category=FailureCategory.BROWSER_CRASH,
            )

    async def stop(self) -> ActionResult:
        """Stop the session. Idempotent. Does NOT auto-stop on kill-switch trip
        (per Point 3 decision — leaves the browser intact for debugging)."""
        if not self._started or self._sb is None:
            self._started = False
            return ok_result(data={"already_stopped": True})
        try:
            await self._sb.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Error during SessionManager.stop(): %r", exc)
        finally:
            self._sb = None
            self._started = False
        return ok_result(data={"stopped": True})

    # -- accessors -----------------------------------------------------------

    @property
    def sb(self) -> Optional[SuperBrowser]:
        """The live Super-Browser facade. None if not started.

        Intended for trusted internal callers only (dispatcher, broker setup).
        """
        return self._sb

    @property
    def started(self) -> bool:
        return self._started

    # -- internals -----------------------------------------------------------

    def _build_default_config(self) -> SBConfig:
        """Build a Super-Browser Config for attach mode.

        Attach reuses the user's logged-in Chrome. We disable LLM-driven
        subsystems (agent loop, recovery, budget) that are not needed for the
        Phase 0a read-only substrate and would pull in extra dependencies.
        """
        mode_str = self._ww_config.session_mode_override
        mode = SessionMode(mode_str) if mode_str else SessionMode.PATCHRIGHT_ATTACH

        session = SessionConfig(
            mode=mode,
            headless=False,  # attach is headed by definition
        )

        # Build the top-level Config with the composed browser session and
        # disabled optional subsystems. SBConfig fields are documented in
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
