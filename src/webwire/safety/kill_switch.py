"""Kill switch — capability execution stop, not a browser-process destructor.

Per the Phase 0a design (Point 3 decision):
- Two active mechanisms: an in-process flag + a hot file (``.webwire/kill``).
  An env var is a boot-time default only — it is NOT dynamically observable
  in a running process, so it cannot be the active kill mechanism.
- Checked at TWO sites: (1) top of the dispatcher before resolving a capability,
  (2) every broker method entry, so a long capability cannot continue browser
  operations after the switch trips.
- Tripping does NOT call ``sb.stop()``. The browser is left intact for
  debugging. Kill switch = refuse new Agent-WebWire actions.

M5 layer 3 adds trip listeners. The Commit Gateway binds the authorization
epoch to this hook so a trip revokes already-minted execution authority even
if the operator later resets the kill switch. Hot-file trips are detected on
the next ``tripped()`` observation. Notification is tracked per listener, so a
listener registered while a trip is already active is notified immediately.
"""

from __future__ import annotations

import logging
import os
from typing import Callable, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, kill_switched

logger = logging.getLogger(__name__)

__all__ = ["KillSwitch"]


class KillSwitch:
    """Tri-state kill switch with hot-file + in-process flag + boot env var."""

    def __init__(self, config: Optional[WebWireConfig] = None) -> None:
        self._config = config or WebWireConfig()
        self._flag: bool = False
        self._trip_listeners: list[Callable[[], object]] = []
        self._notified_listeners: list[Callable[[], object]] = []
        env_var = self._config.kill_env_var
        if env_var and os.environ.get(env_var, "").lower() in ("1", "true", "yes"):
            self._flag = True
            logger.warning(
                "KillSwitch tripped at boot via env var %s=%r",
                env_var, os.environ.get(env_var),
            )

    # -- trip listeners ------------------------------------------------------

    def add_trip_listener(self, listener: Callable[[], object]) -> None:
        """Invoke ``listener`` once for the current trip and once per future trip.

        If the switch is already active when a new listener is registered, that
        listener is notified immediately even if earlier listeners were already
        notified. This is required for authorization-epoch revocation when a
        CommitGateway is constructed during an existing trip.
        """
        if listener not in self._trip_listeners:
            self._trip_listeners.append(listener)
        self.tripped()

    def _notify_active_listeners(self) -> None:
        for listener in tuple(self._trip_listeners):
            if listener in self._notified_listeners:
                continue
            listener()
            self._notified_listeners.append(listener)

    # -- query ---------------------------------------------------------------

    def tripped(self) -> bool:
        """True if the switch is currently tripped (flag OR hot file)."""
        if self._flag:
            active = True
        else:
            try:
                active = self._config.kill_path().exists()
            except OSError:
                active = True

        if active:
            self._notify_active_listeners()
        else:
            self._notified_listeners.clear()
        return active

    def state(self) -> dict:
        """Diagnostic snapshot of both mechanisms. For health/journal."""
        path = self._config.kill_path()
        return {
            "tripped": self.tripped(),
            "flag": self._flag,
            "hot_file": str(path),
            "hot_file_exists": path.exists() if path.parent.exists() else None,
            "env_var": self._config.kill_env_var,
        }

    # -- mutations -----------------------------------------------------------

    def trip(self) -> None:
        """Trip the flag and touch the hot file; notify each listener once."""
        self._flag = True
        path = self._config.kill_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch()
        except OSError as exc:
            logger.warning("Could not write kill hot file %s: %r", path, exc)
        self._notify_active_listeners()
        logger.warning("KillSwitch tripped")

    def reset(self) -> None:
        """Clear the flag and hot file; authorization epochs never move back."""
        self._flag = False
        path = self._config.kill_path()
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            logger.warning("Could not remove kill hot file %s: %r", path, exc)
        self.tripped()
        logger.info("KillSwitch reset")

    # -- guard helper --------------------------------------------------------

    def guard(self) -> Optional[ActionResult]:
        """Return a kill-switched ActionResult if tripped, else None."""
        if self.tripped():
            return kill_switched()
        return None
