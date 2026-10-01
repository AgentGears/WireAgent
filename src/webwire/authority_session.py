"""M7 Layer 2 — the authority session and service lifecycle fence.

One frozen topology (M7_DESIGN.md build order + §24.1):

    canonical absolute state_dir
            ↓
    AuthorityOwnerLock            (Layer 1)
            ↓
    AuthoritySession              (this module)
            ↓
    AuthorityServiceLifecycle     STARTING → READY → DRAINING → TERMINAL
            ↓
    one Dispatcher / M5 / M6 authority root

The session is the single synchronized lifecycle fence for the owner
process: startup, request admission, recovery admission, and drain share
one ordering while ownership remains held.

Invariants (frozen):

- **Admission is state-gated.** Work may be admitted only while the
  session is READY. STARTING admits nothing; DRAINING and TERMINAL refuse
  new work; a TERMINAL session never becomes active again.
- **Drain is owner-wide.** ``begin_drain()`` closes admission; every
  admitted unit of owner work — Dispatcher invocations AND reconciliation
  operator operations — decrements the same active count; drain completes
  only when that count reaches zero.
- **Ephemeral authority dies with the session.** Revokers registered on
  the session (confirmation-epoch advancement, reconciliation retirement)
  run before the session is marked TERMINAL and before the owner lock is
  released, so no pending confirmation or reconciliation authority
  survives controlled ownership replacement.
- **Release is last.** The session does not own the lock itself; the
  composition (drain → revoke → retire → terminalize → close lock) is the
  caller's shutdown law, enforced by the Dispatcher integration and the
  offline recovery owner.

This module is stdlib-only so the rules path stays browser-independent.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "AuthoritySessionState",
    "AuthoritySessionError",
    "AuthorityAdmissionClosedError",
    "AuthoritySession",
]


class AuthoritySessionState(StrEnum):
    STARTING = "starting"
    READY = "ready"
    DRAINING = "draining"
    TERMINAL = "terminal"


class AuthoritySessionError(RuntimeError):
    """The authority-session lifecycle contract was violated."""


class AuthorityAdmissionClosedError(AuthoritySessionError):
    """New owner work was refused: the session is not admitting."""


class AuthoritySession:
    """One owner session over a held authority domain.

    Created in STARTING when ownership is acquired; ``activate()`` moves it
    to READY. Terminalization is permanent: a terminal session never
    becomes active again (build a new session over a new acquisition).
    """

    def __init__(self, *, authority_domain: Any) -> None:
        self._authority_domain = authority_domain
        self._condition = threading.Condition(threading.RLock())
        self._state = AuthoritySessionState.STARTING
        self._active = 0
        self._revokers: list[Callable[[], None]] = []
        self._revoked = False

    @property
    def authority_domain(self) -> Any:
        return self._authority_domain

    @property
    def state(self) -> AuthoritySessionState:
        with self._condition:
            return self._state

    @property
    def active_work(self) -> int:
        with self._condition:
            return self._active

    @property
    def admitting(self) -> bool:
        with self._condition:
            return self._state is AuthoritySessionState.READY

    # -- lifecycle transitions ---------------------------------------------

    def activate(self) -> None:
        """STARTING → READY: the authority root is live and may admit work."""
        with self._condition:
            if self._state is not AuthoritySessionState.STARTING:
                raise AuthoritySessionError(f"authority session cannot activate from {self._state.value}")
            self._state = AuthoritySessionState.READY
            self._condition.notify_all()

    def begin_drain(self) -> None:
        """READY → DRAINING: new admission is refused from this point on."""
        with self._condition:
            if self._state is not AuthoritySessionState.READY:
                raise AuthoritySessionError(f"authority session cannot drain from {self._state.value}")
            self._state = AuthoritySessionState.DRAINING
            self._condition.notify_all()

    def wait_drained(self, timeout: Optional[float] = None) -> bool:
        """Block until every admitted unit of owner work has completed.

        Returns False on timeout (the caller decides how to fail closed)."""
        with self._condition:
            return self._condition.wait_for(lambda: self._active == 0, timeout=timeout)

    def register_revoker(self, revoker: Callable[[], None]) -> None:
        """Register ephemeral-authority revocation (e.g. confirmation-epoch
        advancement). Revokers run exactly once, before terminalization."""
        with self._condition:
            self._revokers.append(revoker)

    def revoke_authority(self) -> None:
        """Run every registered revoker. Idempotent; safe to call during
        DRAINING. Pending owner-side confirmation/reconciliation authority
        does not outlive this call."""
        with self._condition:
            if self._revoked:
                return
            self._revoked = True
            revokers = list(self._revokers)
        # Revokers run OUTSIDE the condition lock: they may take their own
        # locks (e.g. the ConfirmationState lock) and must not deadlock
        # against an admission exit that needs this condition.
        for revoker in revokers:
            revoker()

    def terminalize(self) -> None:
        """DRAINING → TERMINAL (permanent). Revokes authority first."""
        self.revoke_authority()
        with self._condition:
            if self._state is AuthoritySessionState.TERMINAL:
                return
            if self._state is not AuthoritySessionState.DRAINING:
                raise AuthoritySessionError(
                    f"authority session cannot terminalize from {self._state.value} (drain first)"
                )
            self._state = AuthoritySessionState.TERMINAL
            self._condition.notify_all()

    # -- admission -----------------------------------------------------------

    @contextmanager
    def admit(self) -> Iterator[None]:
        """Admit one unit of owner work while READY.

        Raises AuthorityAdmissionClosedError when the session is not
        admitting (STARTING / DRAINING / TERMINAL / no session). The count
        is the owner-wide drain barrier: stop() drains on it."""
        with self._condition:
            if self._state is not AuthoritySessionState.READY:
                raise AuthorityAdmissionClosedError(
                    f"authority session is {self._state.value}: new owner work is refused"
                )
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
