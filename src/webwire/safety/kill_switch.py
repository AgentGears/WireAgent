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

Programmatic trip/reset and M5 authority crossing share the state lock exposed
by :meth:`execution_fence`. This gives them a process-local linearization point:
either an authority boundary completes before trip activation, or the trip
activates (and listeners bump the epoch) before the authority check. External
hot-file creation cannot participate in a Python lock, so it retains the
existing check-at-observation semantics.
"""

from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager
from typing import Callable, Iterator, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, kill_switched

logger = logging.getLogger(__name__)

__all__ = ["KillSwitch"]


class KillSwitch:
    """Tri-state kill switch with hot-file + in-process flag + boot env var."""

    def __init__(self, config: Optional[WebWireConfig] = None) -> None:
        self._config = config or WebWireConfig()
        self._state_lock = threading.RLock()
        self._flag: bool = False
        self._trip_listeners: list[Callable[[], object]] = []
        self._notified_listeners: list[Callable[[], object]] = []
        # Re-entrant listener callbacks may call tripped()/trip()/add_listener()
        # while this RLock is held. Track callbacks currently executing so the
        # nested refresh skips them instead of recursively invoking them again.
        self._listeners_in_progress: list[Callable[[], object]] = []
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
        with self._state_lock:
            if listener not in self._trip_listeners:
                self._trip_listeners.append(listener)
            self._refresh_trip_unlocked()

    def _notify_active_listeners_unlocked(self) -> None:
        """Notify each listener at most once per active trip.

        A callback is marked in-progress *before* invocation so a re-entrant
        call to ``tripped()``, ``trip()``, or ``add_trip_listener()`` cannot
        invoke the same callback recursively through the RLock. Successful
        listeners become notified for the current active trip. Failed listeners
        are removed from the in-progress marker and remain unnotified so a later
        observation can retry them. One failure never blocks later listeners.
        Caller must hold ``_state_lock``.
        """
        for listener in tuple(self._trip_listeners):
            # A trusted callback can re-enter and reset the switch. If that
            # happens, stop delivering callbacks for the trip that no longer
            # exists; a future trip starts a fresh notification cycle.
            if not self._active_unlocked():
                break
            if (
                listener in self._notified_listeners
                or listener in self._listeners_in_progress
            ):
                continue

            self._listeners_in_progress.append(listener)
            succeeded = False
            try:
                listener()
                succeeded = True
            except Exception:
                logger.exception("KillSwitch trip listener failed: %r", listener)
            finally:
                # Remove only this invocation's marker. The list form avoids
                # imposing hashability requirements on arbitrary callables.
                if listener in self._listeners_in_progress:
                    self._listeners_in_progress.remove(listener)

            # A callback is allowed to re-enter the switch and even reset it.
            # Only mark it notified when the trip is still active afterwards;
            # otherwise the next future trip must be able to notify it again.
            if (
                succeeded
                and self._active_unlocked()
                and listener not in self._notified_listeners
            ):
                self._notified_listeners.append(listener)

    def _active_unlocked(self) -> bool:
        if self._flag:
            return True
        try:
            return self._config.kill_path().exists()
        except OSError:
            # If state cannot be observed reliably, fail closed.
            return True

    def _refresh_trip_unlocked(self) -> bool:
        """Refresh active state/listeners. Caller must hold ``_state_lock``."""
        active = self._active_unlocked()
        if active:
            self._notify_active_listeners_unlocked()
        else:
            self._notified_listeners.clear()
        return active

    # -- query ---------------------------------------------------------------

    def tripped(self) -> bool:
        """True if the switch is currently tripped (flag OR hot file)."""
        with self._state_lock:
            return self._refresh_trip_unlocked()

    @contextmanager
    def execution_fence(self) -> Iterator[bool]:
        """Hold kill state stable across one in-process authority boundary.

        The yielded boolean is the current trip state. While the context is
        held, :meth:`trip` and :meth:`reset` cannot interleave with the caller.
        This is the synchronization primitive used by M5's CommitGateway for
        authorization and permit consumption. It deliberately cannot serialize
        an external process creating the hot file; that source remains governed
        by boundary-time observation.
        """
        with self._state_lock:
            yield self._refresh_trip_unlocked()

    def state(self) -> dict:
        """Diagnostic snapshot of both mechanisms. For health/journal."""
        with self._state_lock:
            path = self._config.kill_path()
            active = self._refresh_trip_unlocked()
            try:
                hot_file_exists = path.exists() if path.parent.exists() else None
            except OSError:
                hot_file_exists = None
            return {
                "tripped": active,
                "flag": self._flag,
                "hot_file": str(path),
                "hot_file_exists": hot_file_exists,
                "env_var": self._config.kill_env_var,
            }

    # -- mutations -----------------------------------------------------------

    def trip(self) -> None:
        """Atomically activate the in-process trip and notify listeners.

        The state lock is also held by CommitGateway authority boundaries. A
        concurrent boundary therefore linearizes wholly before or wholly after
        this activation; it cannot pass a stale kill/epoch check and then cross
        after the trip becomes active.
        """
        with self._state_lock:
            self._flag = True
            path = self._config.kill_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError as exc:
                logger.warning("Could not write kill hot file %s: %r", path, exc)
            self._notify_active_listeners_unlocked()
        logger.warning("KillSwitch tripped")

    def reset(self) -> None:
        """Clear the flag and hot file; authorization epochs never move back."""
        with self._state_lock:
            self._flag = False
            path = self._config.kill_path()
            try:
                if path.exists():
                    path.unlink()
            except OSError as exc:
                logger.warning("Could not remove kill hot file %s: %r", path, exc)
            self._refresh_trip_unlocked()
        logger.info("KillSwitch reset")

    # -- guard helper --------------------------------------------------------

    def guard(self) -> Optional[ActionResult]:
        """Return a kill-switched ActionResult if tripped, else None."""
        if self.tripped():
            return kill_switched()
        return None
