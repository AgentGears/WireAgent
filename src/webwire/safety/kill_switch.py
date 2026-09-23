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
the next ``tripped()`` observation. Listener delivery is tied to a monotonically
increasing trip generation, so re-entrant reset/retrip cannot make a callback
from one activation count as notification for a later activation.

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
        self._listeners_in_progress: list[Callable[[], object]] = []
        # ``_last_active`` + ``_trip_generation`` identify distinct observed
        # inactive -> active transitions. The generation is the once-per-trip
        # identity; a callback may reset and retrip while the RLock is re-entered.
        self._last_active = False
        self._trip_generation = 0
        # Nested refreshes during a callback update active/generation state but
        # never start a second notification traversal. The outer traversal drains
        # all pending listeners for the newest generation after the callback exits.
        self._notifying = False
        env_var = self._config.kill_env_var
        if env_var and os.environ.get(env_var, "").lower() in ("1", "true", "yes"):
            self._flag = True
            logger.warning(
                "KillSwitch tripped at boot via env var %s=%r",
                env_var, os.environ.get(env_var),
            )

    # -- trip listeners ------------------------------------------------------

    def add_trip_listener(self, listener: Callable[[], object]) -> None:
        """Invoke ``listener`` once for the current trip and once per future trip."""
        with self._state_lock:
            if listener not in self._trip_listeners:
                self._trip_listeners.append(listener)
            self._refresh_trip_unlocked()

    def _active_unlocked(self) -> bool:
        if self._flag:
            return True
        try:
            return self._config.kill_path().exists()
        except OSError:
            # If state cannot be observed reliably, fail closed.
            return True

    def _sync_trip_generation_unlocked(self) -> bool:
        """Observe active state and advance generation on a fresh activation."""
        active = self._active_unlocked()
        if active and not self._last_active:
            self._trip_generation += 1
            self._notified_listeners.clear()
        elif not active and self._last_active:
            self._notified_listeners.clear()
        self._last_active = active
        return active

    def _notify_active_listeners_unlocked(self) -> None:
        """Drain pending callbacks for the current trip generation.

        Only the outermost notification traversal invokes callbacks. Re-entrant
        ``tripped()``, ``trip()``, ``reset()``, or ``add_trip_listener()`` calls
        may update active/generation state, but they cannot recursively invoke a
        callback. If a callback resets and retrips, its invocation belongs only
        to the generation captured before it ran; the traversal restarts and
        delivers it again for the new generation.

        A failed callback is attempted at most once per traversal/generation.
        It remains unnotified, so a later external observation retries it, while
        successful later listeners in the same trip are still allowed to run.
        Caller must hold ``_state_lock``.
        """
        if self._notifying:
            return

        self._notifying = True
        attempted: list[Callable[[], object]] = []
        attempted_generation = -1
        try:
            while True:
                if not self._sync_trip_generation_unlocked():
                    return
                generation = self._trip_generation
                if generation != attempted_generation:
                    attempted.clear()
                    attempted_generation = generation

                pending = [
                    listener
                    for listener in tuple(self._trip_listeners)
                    if listener not in self._notified_listeners
                    and listener not in self._listeners_in_progress
                    and listener not in attempted
                ]
                if not pending:
                    return

                listener = pending[0]
                attempted.append(listener)
                self._listeners_in_progress.append(listener)
                succeeded = False
                try:
                    listener()
                    succeeded = True
                except Exception:
                    logger.exception("KillSwitch trip listener failed: %r", listener)
                finally:
                    if listener in self._listeners_in_progress:
                        self._listeners_in_progress.remove(listener)

                active = self._sync_trip_generation_unlocked()
                if not active:
                    return
                if self._trip_generation != generation:
                    # reset()+trip() occurred during the callback. The callback
                    # just completed for the old generation and is still pending
                    # for the newly-created generation; loop restart clears the
                    # per-generation attempted set and delivers it again.
                    continue
                if succeeded and listener not in self._notified_listeners:
                    self._notified_listeners.append(listener)
        finally:
            self._notifying = False

    def _refresh_trip_unlocked(self) -> bool:
        """Refresh active generation and listeners. Caller holds ``_state_lock``."""
        active = self._sync_trip_generation_unlocked()
        if active and not self._notifying:
            self._notify_active_listeners_unlocked()
            active = self._sync_trip_generation_unlocked()
        return active

    # -- query ---------------------------------------------------------------

    def tripped(self) -> bool:
        """True if the switch is currently tripped (flag OR hot file)."""
        with self._state_lock:
            return self._refresh_trip_unlocked()

    @contextmanager
    def execution_fence(self) -> Iterator[bool]:
        """Hold kill state stable across one in-process authority boundary."""
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
        """Atomically activate the in-process trip and notify this generation."""
        with self._state_lock:
            self._flag = True
            path = self._config.kill_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError as exc:
                logger.warning("Could not write kill hot file %s: %r", path, exc)
            self._refresh_trip_unlocked()
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
