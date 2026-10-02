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

import secrets
import threading
import time
from contextlib import contextmanager
from enum import StrEnum
from typing import Any, Callable, Iterator, Optional

__all__ = [
    "AuthoritySessionState",
    "AuthoritySessionError",
    "AuthorityAdmissionClosedError",
    "AuthorityStaleInstanceError",
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


class AuthorityStaleInstanceError(AuthorityAdmissionClosedError):
    """Admission carried the instance id of a PREVIOUS owner session.

    Distinct from a closed session: the caller's expected owner is not
    this owner. Layer 4's transport will surface this as the
    stale-instance refusal; nothing about the admission counter, active
    work, or durable state changes — the rejection happens BEFORE any of
    them (M7 Layer 3, frozen contract item 2)."""


class AuthoritySession:
    """One owner session over a held authority domain.

    Created in STARTING when ownership is acquired; ``activate()`` moves it
    to READY. Terminalization is permanent: a terminal session never
    becomes active again (build a new session over a new acquisition).
    """

    def __init__(self, *, authority_domain: Any) -> None:
        self._authority_domain = authority_domain
        # M7 Layer 3 / frozen contract item 1: a fresh cryptographically
        # random 256-bit owner instance identity, minted before READY and
        # immutable for this session. Diagnostic/protocol identity ONLY —
        # never M5/M6 lineage, never persisted, never recovered from disk.
        # Clean reacquisition and crash takeover both produce a different
        # id because each acquisition constructs a new session.
        self._instance_id = secrets.token_hex(32)
        self._acquired_at = time.time()
        self._condition = threading.Condition(threading.RLock())
        self._state = AuthoritySessionState.STARTING
        self._active = 0
        self._revokers: list[Callable[[], None]] = []
        # F-57/F-59: revocation is a lifecycle sub-state/transaction, not
        # just a completed-set. _started closes activation/admission
        # permanently; _running makes execution single-flight (concurrent
        # callers join the in-flight operation instead of rerunning
        # pending revokers); _complete means every required revoker
        # SUCCEEDED. TERMINAL is unreachable until _complete.
        self._revoker_success: set[int] = set()
        self._revocation_started = False
        self._revocation_running = False
        self._revocation_complete = False

    @property
    def authority_domain(self) -> Any:
        return self._authority_domain

    @property
    def authority_instance_id(self) -> str:
        """The 256-bit hex identity of THIS owner session (read-only)."""
        return self._instance_id

    @property
    def acquired_at(self) -> float:
        """Provenance timestamp of this session's construction (epoch
        seconds; diagnostic only, not a lease — no TTL derives from it)."""
        return self._acquired_at

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
        """STARTING → READY: the authority root is live and may admit work.

        F-59: once revocation has begun (a failed-start teardown started
        closing this session), activation is permanently impossible — the
        abort/activate race can never produce a READY session carrying
        completed or partial revocation."""
        with self._condition:
            if self._revocation_started:
                raise AuthoritySessionError(
                    "cannot activate: revocation has already begun during "
                    "STARTING teardown — this session can only terminalize"
                )
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
        advancement). Revokers run before terminalization, and revocation
        must COMPLETE (all revokers succeed) before the owner handle may
        close.

        F-57: registering a NEW revocation obligation after revocation has
        already completed is refused — the "authority died with this
        session" guarantee would otherwise be silently incomplete for the
        late registration. F-59: registering while a revocation pass is
        EXECUTING is also refused (racy against the in-flight snapshot);
        registering between retries of an incomplete revocation stays
        legal and the retry picks the obligation up."""
        with self._condition:
            if self._revocation_complete or self._revocation_running:
                raise AuthoritySessionError(
                    "cannot register a revoker now: revocation has "
                    "completed or is executing; the owner is shutting down"
                )
            self._revokers.append(revoker)

    def revoke_authority(self) -> None:
        """Run every registered revoker to COMPLETION (F-57/F-59).

        Lifecycle-atomic: revocation is legal only from STARTING
        (failed-start teardown) or DRAINING (shutdown) — calling it on a
        freely admitting READY session is REFUSED with zero side effects,
        because authority minted after a premature revocation could
        otherwise survive the final shutdown boundary. Beginning
        revocation permanently blocks activation and admission for this
        session.

        Single-flight (F-59): only one caller executes revokers at a
        time; concurrent callers JOIN the in-flight operation (they wait
        on the condition and re-check completion) rather than rerunning
        pending callbacks. A raising revoker leaves revocation
        INCOMPLETE and retryable; succeeded revokers are never executed
        twice, including under concurrency. Pending owner-side
        confirmation/reconciliation authority does not outlive a
        COMPLETED call."""
        while True:
            with self._condition:
                if self._revocation_complete:
                    return
                while self._revocation_running:
                    # Join the in-flight revocation pass: never rerun
                    # pending revokers beside the executing caller.
                    self._condition.wait()
                    if self._revocation_complete:
                        return
                if self._state is AuthoritySessionState.READY:
                    raise AuthoritySessionError(
                        "cannot revoke authority on a freely admitting "
                        "READY session — revocation closes a session "
                        "(begin_drain or abort first); refused with zero "
                        "side effects"
                    )
                if self._state is AuthoritySessionState.TERMINAL:
                    # TERMINAL requires completed revocation by
                    # construction; treat as the completed no-op.
                    self._revocation_complete = True
                    return
                # Legal: STARTING (failed-start teardown) or DRAINING
                # (shutdown). Beginning revocation permanently closes this
                # session (F-59): activate() and admission can never
                # succeed afterwards.
                self._revocation_started = True
                self._revocation_running = True
                pending = [
                    (i, revoker) for i, revoker in enumerate(self._revokers) if i not in self._revoker_success
                ]
            try:
                # Revokers run OUTSIDE the condition lock: they may take
                # their own locks (e.g. the ConfirmationState lock) and
                # must not deadlock against an admission exit that needs
                # this condition. A raising revoker propagates: the
                # finally releases single-flight and marks completion only
                # when EVERY required revoker has succeeded.
                for i, revoker in pending:
                    revoker()
                    with self._condition:
                        self._revoker_success.add(i)
            finally:
                with self._condition:
                    self._revocation_running = False
                    if len(self._revoker_success) >= len(self._revokers):
                        self._revocation_complete = True
                    self._condition.notify_all()
            # Loop: re-derive pending (a mid-run late registration or a
            # retry after a raised revoker both converge here; completion
            # short-circuits the next entry).

    def terminalize(self) -> None:
        """DRAINING → TERMINAL (permanent).

        F-57: the lifecycle transition is validated FIRST — an illegal
        call has ZERO revocation side effects. Revocation runs only for a
        legal transition, and TERMINAL is entered only after revocation
        COMPLETED (all revokers succeeded)."""
        with self._condition:
            if self._state is AuthoritySessionState.TERMINAL:
                return
            if self._state is not AuthoritySessionState.DRAINING:
                raise AuthoritySessionError(
                    f"authority session cannot terminalize from {self._state.value} (drain first)"
                )
        self.revoke_authority()  # must COMPLETE before TERMINAL is entered
        with self._condition:
            if self._state is AuthoritySessionState.TERMINAL:
                return
            if self._state is not AuthoritySessionState.DRAINING:
                raise AuthoritySessionError(
                    f"authority session cannot terminalize from {self._state.value} (drain first)"
                )
            self._state = AuthoritySessionState.TERMINAL
            self._condition.notify_all()

    def abort_from_starting(self) -> None:
        """STARTING → TERMINAL for a FAILED startup (F-52).

        F-57: the transition is validated FIRST — an illegal abort has
        zero revocation side effects. Only valid from STARTING, after the
        caller has proven the partially constructed root quiescent; a
        clean failed start still terminalizes before the owner handle
        closes, so no Dispatcher ever reports a live session it no longer
        owns."""
        with self._condition:
            if self._state is AuthoritySessionState.TERMINAL:
                return
            if self._state is not AuthoritySessionState.STARTING:
                raise AuthoritySessionError(
                    f"authority session cannot abort from {self._state.value} "
                    "(abort is the failed-startup transition from STARTING)"
                )
        self.revoke_authority()  # must COMPLETE before TERMINAL is entered
        with self._condition:
            if self._state is AuthoritySessionState.TERMINAL:
                return
            if self._state is not AuthoritySessionState.STARTING:
                raise AuthoritySessionError(
                    f"authority session cannot abort from {self._state.value} "
                    "(abort is the failed-startup transition from STARTING)"
                )
            self._state = AuthoritySessionState.TERMINAL
            self._condition.notify_all()

    # -- admission -----------------------------------------------------------

    @contextmanager
    def admit(self, expected_instance_id: Optional[str] = None) -> Iterator[None]:
        """Admit one unit of owner work while READY.

        Raises AuthorityAdmissionClosedError when the session is not
        admitting (STARTING / DRAINING / TERMINAL / no session). With an
        ``expected_instance_id`` (the Layer-4 caller's stale-session
        check), the comparison happens in THIS same lifecycle critical
        section — a wrong/old id raises AuthorityStaleInstanceError
        BEFORE the active-work counter increments, before any execution,
        confirmation handling, reconciliation, or durable mutation (M7
        Layer 3, frozen contract item 2). Without an expected id this is
        the internal local-owner admission path.

        The count is the owner-wide drain barrier: stop() drains on it."""
        with self._condition:
            if self._state is not AuthoritySessionState.READY:
                raise AuthorityAdmissionClosedError(
                    f"authority session is {self._state.value}: new owner work is refused"
                )
            if expected_instance_id is not None and expected_instance_id != self._instance_id:
                raise AuthorityStaleInstanceError(
                    "stale authority instance: admission expected a previous "
                    "owner session; this owner's instance differs"
                )
            self._active += 1
        try:
            yield
        finally:
            with self._condition:
                self._active -= 1
                self._condition.notify_all()
