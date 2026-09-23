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
epoch to a *critical* listener so a trip revokes already-minted execution
authority even if the operator later resets the kill switch. Hot-file trips are
detected on the next observation.

Trip notification is event-based, not merely derived from the current boolean
state. Each observed inactive -> active transition creates a monotonically
increasing trip generation. Every listener registered for that generation keeps
an obligation to receive it exactly once, even if an earlier listener resets or
retrips the switch while callbacks are being delivered. Failed callbacks remain
owed and are retried only on a later observation.

Callbacks execute outside the kill-state lock. This prevents arbitrary listener
code from creating a cross-thread lock-order inversion with the CommitGateway.
Critical listener obligations are fail-closed: :meth:`execution_fence` denies
remote authority while any critical trip generation remains undelivered. Thus
moving callbacks out of the lock does not create a reset-before-revocation
window.

Programmatic trip/reset and M5 authority crossing still share the state lock for
the active-state transition itself. External hot-file creation cannot participate
in a Python lock, so it retains check-at-observation semantics; once observed it
also creates a generation and the same critical-delivery fence applies.
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
    """Hot-file + in-process kill state with generation-based notifications."""

    def __init__(self, config: Optional[WebWireConfig] = None) -> None:
        self._config = config or WebWireConfig()
        self._state_lock = threading.RLock()
        self._flag: bool = False

        # These arrays are index-aligned. A listener's generation value is the
        # latest trip generation successfully delivered to it. There is no
        # listener-removal API, so indices remain stable for process lifetime.
        self._trip_listeners: list[Callable[[], object]] = []
        self._listener_generations: list[int] = []
        self._listener_critical: list[bool] = []
        self._listeners_in_progress: set[int] = set()

        self._last_active = False
        self._trip_generation = 0
        self._notifying = False

        env_var = self._config.kill_env_var
        if env_var and os.environ.get(env_var, "").lower() in ("1", "true", "yes"):
            self._flag = True
            logger.warning(
                "KillSwitch tripped at boot via env var %s=%r",
                env_var,
                os.environ.get(env_var),
            )

    # -- state / generation --------------------------------------------------

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

    def _has_pending_critical_events_unlocked(self) -> bool:
        return any(
            critical and delivered < self._trip_generation
            for delivered, critical in zip(
                self._listener_generations,
                self._listener_critical,
            )
        )

    def _start_delivery_unlocked(self) -> bool:
        """Claim ownership of the notification drain. Caller holds state lock."""
        if self._notifying or not self._has_pending_listener_events_unlocked():
            return False
        self._notifying = True
        return True

    # -- trip listeners ------------------------------------------------------

    def add_trip_listener(
        self,
        listener: Callable[[], object],
        *,
        critical: bool = False,
    ) -> None:
        """Register for the current active trip (if any) and all future trips.

        ``critical=True`` means undelivered generations block
        :meth:`execution_fence`. CommitGateway uses this for authorization-epoch
        revocation. Generic/diagnostic listeners remain non-critical so their
        failure cannot indefinitely block execution after revocation succeeds.
        """
        with self._state_lock:
            active = self._sync_trip_generation_unlocked()
            try:
                index = self._trip_listeners.index(listener)
            except ValueError:
                self._trip_listeners.append(listener)
                delivered = self._trip_generation - 1 if active else self._trip_generation
                self._listener_generations.append(delivered)
                self._listener_critical.append(critical)
            else:
                if critical:
                    self._listener_critical[index] = True
            should_drain = self._start_delivery_unlocked()

        if should_drain:
            self._drain_pending_listener_events()

    def _select_pending_event_unlocked(
        self,
        attempted: list[tuple[int, int]],
    ) -> Optional[tuple[int, int, Callable[[], object]]]:
        """Select the oldest undelivered listener-generation obligation."""
        candidate: Optional[tuple[int, int, Callable[[], object]]] = None
        for index, listener in enumerate(tuple(self._trip_listeners)):
            delivered = self._listener_generations[index]
            if delivered >= self._trip_generation or index in self._listeners_in_progress:
                continue
            target_generation = delivered + 1
            if (index, target_generation) in attempted:
                continue
            if candidate is None or target_generation < candidate[1]:
                candidate = (index, target_generation, listener)
        return candidate

    def _drain_pending_listener_events(self) -> None:
        """Deliver owed trip events with *no* arbitrary callback under state lock.

        The caller must have set ``_notifying=True`` through
        :meth:`_start_delivery_unlocked`. Re-entrant API calls may create newer
        generations or append listeners; the outer drain observes them on its
        next locked selection pass. A failed event is attempted only once in this
        drain and remains owed for a later external observation.
        """
        attempted: list[tuple[int, int]] = []
        try:
            while True:
                with self._state_lock:
                    self._sync_trip_generation_unlocked()
                    candidate = self._select_pending_event_unlocked(attempted)
                    if candidate is None:
                        return
                    index, target_generation, listener = candidate
                    attempted.append((index, target_generation))
                    self._listeners_in_progress.add(index)

                succeeded = False
                try:
                    listener()
                    succeeded = True
                except Exception:
                    logger.exception("KillSwitch trip listener failed: %r", listener)
                finally:
                    with self._state_lock:
                        self._listeners_in_progress.discard(index)
                        self._sync_trip_generation_unlocked()
                        if succeeded:
                            current = self._listener_generations[index]
                            if current < target_generation:
                                self._listener_generations[index] = target_generation
        finally:
            with self._state_lock:
                self._notifying = False

    def _observe_and_drain(self) -> bool:
        """Observe state, publish generations, and drain callbacks outside lock."""
        with self._state_lock:
            active = self._sync_trip_generation_unlocked()
            should_drain = self._start_delivery_unlocked()
        if should_drain:
            self._drain_pending_listener_events()
        with self._state_lock:
            return self._sync_trip_generation_unlocked()

    # -- query ---------------------------------------------------------------

    def tripped(self) -> bool:
        """True if currently tripped; also advances pending listener delivery."""
        return self._observe_and_drain()

    @contextmanager
    def execution_fence(self) -> Iterator[bool]:
        """Hold kill state stable across one in-process authority boundary.

        Listener callbacks are deliberately *not* invoked here because the
        caller may already hold the CommitGateway protocol lock. Critical
        pending generations instead make the fence fail closed. CommitGateway
        performs a listener-draining preflight before acquiring its protocol
        lock, while this fence closes the race between that preflight and the
        authority transition.
        """
        with self._state_lock:
            active = self._sync_trip_generation_unlocked()
            blocked = active or self._has_pending_critical_events_unlocked()
            yield blocked

    def state(self) -> dict:
        """Diagnostic snapshot of both mechanisms. For health/journal."""
        self._observe_and_drain()
        with self._state_lock:
            path = self._config.kill_path()
            active = self._sync_trip_generation_unlocked()
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
                "trip_generation": self._trip_generation,
                "critical_revocation_pending": self._has_pending_critical_events_unlocked(),
            }

    # -- mutations -----------------------------------------------------------

    def trip(self) -> None:
        """Activate the in-process trip, publish its generation, then notify."""
        with self._state_lock:
            self._flag = True
            path = self._config.kill_path()
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.touch()
            except OSError as exc:
                logger.warning("Could not write kill hot file %s: %r", path, exc)
            self._sync_trip_generation_unlocked()
            should_drain = self._start_delivery_unlocked()

        if should_drain:
            self._drain_pending_listener_events()
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
            self._sync_trip_generation_unlocked()
            should_drain = self._start_delivery_unlocked()

        if should_drain:
            self._drain_pending_listener_events()
        logger.info("KillSwitch reset")

    # -- guard helper --------------------------------------------------------

    def guard(self) -> Optional[ActionResult]:
        """Return a kill-switched ActionResult if tripped, else None."""
        if self.tripped():
            return kill_switched()
        return None
