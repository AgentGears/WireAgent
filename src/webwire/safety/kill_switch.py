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
the next ``tripped()`` observation.

Trip notification is event-based, not merely derived from the current boolean
state. Each observed inactive -> active transition creates a monotonically
increasing trip generation. Every listener that was registered for that
generation retains an obligation to receive it exactly once, even if an earlier
listener resets or retrips the switch while callbacks are being delivered.
Failed callbacks remain owed and are retried only on a later observation.

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
        # Aligned with ``_trip_listeners``. Entry i is the latest trip
        # generation successfully delivered to listener i. This is the small
        # process-local event ledger that prevents reset/retrip from erasing a
        # revocation event before all registered listeners observe it.
        self._listener_generations: list[int] = []
        self._listeners_in_progress: list[Callable[[], object]] = []
        self._last_active = False
        self._trip_generation = 0
        # Nested refreshes during callbacks update active/generation state but
        # never recurse into another delivery traversal. The outer traversal
        # drains all outstanding generation obligations after the callback exits.
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
        """Register for the current active trip (if any) and all future trips.

        A listener added while the switch is active owes the current generation
        and is notified immediately. A listener added while inactive begins at
        the current generation and therefore does not receive historical trips.
        """
        with self._state_lock:
            active = self._sync_trip_generation_unlocked()
            if listener not in self._trip_listeners:
                self._trip_listeners.append(listener)
                delivered = self._trip_generation - 1 if active else self._trip_generation
                self._listener_generations.append(delivered)
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
        """Observe state and mint one generation per inactive -> active edge."""
        active = self._active_unlocked()
        if active and not self._last_active:
            self._trip_generation += 1
        self._last_active = active
        return active

    def _has_pending_listener_events_unlocked(self) -> bool:
        return any(
            delivered < self._trip_generation
            for delivered in self._listener_generations
        )

    def _notify_pending_listeners_unlocked(self) -> None:
        """Deliver all currently owed trip-generation events without recursion.

        Delivery is independent of the switch's *current* active state: once a
        generation has existed, its safety side effects (especially authorization
        epoch revocation) remain owed even if another callback resets the switch.

        Only the outermost traversal invokes callbacks. Re-entrant ``tripped()``,
        ``trip()``, ``reset()``, or ``add_trip_listener()`` calls may update the
        active state and create newer generations, but those nested calls cannot
        recursively invoke listeners. A successful callback advances exactly one
        owed generation. A failed callback remains behind and is retried on a
        later external observation, never repeatedly in the same traversal.
        Caller must hold ``_state_lock``.
        """
        if self._notifying:
            return

        self._notifying = True
        attempted: list[tuple[int, int]] = []
        try:
            while True:
                self._sync_trip_generation_unlocked()

                candidate_index: Optional[int] = None
                candidate_generation: Optional[int] = None
                for index, listener in enumerate(tuple(self._trip_listeners)):
                    delivered = self._listener_generations[index]
                    if delivered >= self._trip_generation:
                        continue
                    if listener in self._listeners_in_progress:
                        continue
                    target_generation = delivered + 1
                    if (index, target_generation) in attempted:
                        continue
                    if (
                        candidate_generation is None
                        or target_generation < candidate_generation
                    ):
                        candidate_index = index
                        candidate_generation = target_generation

                if candidate_index is None or candidate_generation is None:
                    return

                listener = self._trip_listeners[candidate_index]
                attempted.append((candidate_index, candidate_generation))
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

                # Re-entry may have changed active state or created a newer trip
                # generation. Capture that before crediting the event we just
                # delivered. The old generation remains valid historical work.
                self._sync_trip_generation_unlocked()
                if succeeded:
                    # Listener ordering is stable because there is no removal API.
                    # Dynamic registration appends and cannot shift this index.
                    current = self._listener_generations[candidate_index]
                    if current < candidate_generation:
                        self._listener_generations[candidate_index] = candidate_generation
        finally:
            self._notifying = False

    def _refresh_trip_unlocked(self) -> bool:
        """Refresh active state and drain pending trip events. Caller holds lock."""
        active = self._sync_trip_generation_unlocked()
        if not self._notifying and self._has_pending_listener_events_unlocked():
            self._notify_pending_listeners_unlocked()
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
        """Atomically activate the in-process trip and publish its generation."""
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
        """Clear active mechanisms; already-created trip events remain owed."""
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
