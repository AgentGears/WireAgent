"""M7 Layer 2 — the offline recovery owner.

Frozen §24.1 requires ownership for BOTH Dispatcher and standalone
recovery entrypoints. When no runtime is live, offline reconciliation
(across a ledger left ambiguous by a crash) must itself acquire the
authority domain: build the M5/M6 authority-root objects UNDER the owner
lock, expose reconciliation through an admitted operator session, and on
close drain owner-wide, revoke, terminalize, and release — the same
shutdown law as the Dispatcher.

A live runtime holding the domain makes offline acquisition fail closed
with authority_busy (the runtime's own operator surface is the path then).
"""

from __future__ import annotations

from dataclasses import replace as _dc_replace
from typing import Any, Optional

from webwire.authority import AuthorityOwnerLock
from webwire.authority_operators import OwnedReconciliationOperatorSession
from webwire.authority_session import AuthoritySession, AuthoritySessionError

__all__ = ["OfflineRecoveryAuthority", "canonical_config"]


def canonical_config(config: Any) -> Any:
    """M7-RV11 / F-46: resolve state_dir ONCE, before any authority-root
    construction, and use that exact canonical domain throughout the owner
    — a later CWD change can never split the lock domain from the safety
    state domain."""
    resolved = config.state_dir.resolve(strict=False)
    if resolved == config.state_dir:
        return config
    return _dc_replace(config, state_dir=resolved)


class OfflineRecoveryAuthority:
    """A standalone recovery owner over one canonical authority domain."""

    def __init__(self, config: Any) -> None:
        self._config = canonical_config(config)
        self._owner_lock: Optional[AuthorityOwnerLock] = None
        self._session: Optional[AuthoritySession] = None
        self._coordinator: Any = None

    @property
    def authority_domain(self) -> Any:
        return self._config.state_dir

    def acquire(self) -> "OfflineRecoveryAuthority":
        """Acquire ownership and build the authority root under it."""
        if self._owner_lock is not None:
            raise AuthoritySessionError("offline recovery owner already acquired")
        from webwire.safety import (
            DEFAULT_EFFECT_POLICIES,
            EffectLedger,
            ReconciliationCoordinator,
            RecoveryGuard,
        )
        from webwire.safety.commit_gateway import CommitGateway
        from webwire.safety.confirmation_state import ConfirmationState
        from webwire.safety.execution_models import AuthorizationEpoch
        from webwire.safety.kill_switch import KillSwitch

        lock = AuthorityOwnerLock(self._config.state_dir).acquire()
        try:
            ledger = EffectLedger(self._config)
            guard = RecoveryGuard(ledger)
            gateway = CommitGateway(
                ledger=ledger,
                kill_switch=KillSwitch(self._config),
                authorization_epoch=AuthorizationEpoch(),
                policies=DEFAULT_EFFECT_POLICIES,
            )
            # A fresh ConfirmationState by design: the successor never
            # restores old ephemeral confirmation authority (M7 completion
            # criteria); offline reconciliation carries none.
            coordinator = ReconciliationCoordinator(
                recovery_guard=guard,
                confirmation_state=ConfirmationState(),
                commit_gateway=gateway,
            )
            session = AuthoritySession(authority_domain=self._config.state_dir)
            session.activate()
        except BaseException:
            lock.release()
            raise
        self._owner_lock = lock
        self._session = session
        self._coordinator = coordinator
        return self

    def operator_session(self, operator_id: str) -> OwnedReconciliationOperatorSession:
        """An admitted reconciliation operator session over this owner."""
        if self._session is None or self._coordinator is None:
            raise AuthoritySessionError("offline recovery owner has not acquired the authority domain")
        return OwnedReconciliationOperatorSession(
            session=self._session,
            operator_id=operator_id,
            delegate_factory=self._make_operator_session,
        )

    def _make_operator_session(self, operator_id: str) -> Any:
        from webwire.safety.reconciliation_operator import (
            ReconciliationOperatorSession,
        )

        return ReconciliationOperatorSession(
            coordinator=self._coordinator,
            operator_id=operator_id,
        )

    def close(self) -> None:
        """The shutdown law: reject admission, drain owner-wide, revoke,
        terminalize, release the owner lock last."""
        session = self._session
        lock = self._owner_lock
        if session is None or lock is None:
            raise AuthoritySessionError("offline recovery owner has nothing to close")
        session.begin_drain()
        if not session.wait_drained(timeout=60.0):
            raise AuthoritySessionError("offline recovery owner failed to drain admitted work")
        session.revoke_authority()
        session.terminalize()
        self._session = None
        self._coordinator = None
        self._owner_lock = None
        lock.release()

    def __enter__(self) -> "OfflineRecoveryAuthority":
        return self.acquire()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self._owner_lock is not None:
            self.close()
