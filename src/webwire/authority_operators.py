"""M7 Layer 2 — owner-gated reconciliation operator access.

Reconciliation is owner-side safety work: it must (a) be creatable only
while a READY authority session admits it (F-43: no non-owner operator
access through a live runtime's exposed coordinator), and (b) count as
admitted owner work in the owner-wide drain (F-48: draining Dispatcher
invocations alone is not a drain).

The wrapper is deliberately thin: it admits through the session and
delegates to the real ReconciliationOperatorSession. It is used by BOTH
the Dispatcher's gated factory and the offline recovery owner.
"""

from __future__ import annotations

from typing import Any, Optional

from webwire.authority_session import (
    AuthorityAdmissionClosedError,
    AuthoritySession,
    AuthoritySessionError,
)

__all__ = ["OwnedReconciliationOperatorSession", "require_ready_session"]


def require_ready_session(
    session: Optional[AuthoritySession],
) -> AuthoritySession:
    """Gate: operator access requires an active (READY) owner session."""
    if session is None:
        raise AuthoritySessionError(
            "no authority session is active: this runtime owns no authority "
            "domain (start it, or use the offline recovery owner)"
        )
    if not session.admitting:
        raise AuthorityAdmissionClosedError(
            f"authority session is {session.state.value}: reconciliation operator access is refused"
        )
    return session


class OwnedReconciliationOperatorSession:
    """A reconciliation operator session whose every operation is admitted
    owner work: creation is gated on a READY session, and each
    prepare/confirm/resolve call holds an admission until it returns, so
    stop() cannot release ownership while reconciliation is in flight."""

    def __init__(
        self,
        *,
        session: AuthoritySession,
        operator_id: str,
        delegate_factory: Any,
    ) -> None:
        require_ready_session(session)
        self._session = session
        self._delegate = delegate_factory(operator_id)

    @property
    def delegate(self) -> Any:
        return self._delegate

    @property
    def _coordinator(self) -> Any:
        """Pass-through: the coordinator this operator session drives."""
        return self._delegate._coordinator

    def prepare_resolution(self, *args: Any, **kwargs: Any) -> Any:
        with self._session.admit():
            return self._delegate.prepare_resolution(*args, **kwargs)

    def confirm_resolution(self, *args: Any, **kwargs: Any) -> Any:
        with self._session.admit():
            return self._delegate.confirm_resolution(*args, **kwargs)

    def resolve(self, *args: Any, **kwargs: Any) -> Any:
        with self._session.admit():
            return self._delegate.resolve(*args, **kwargs)
