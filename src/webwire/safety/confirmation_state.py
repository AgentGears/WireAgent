"""M6 process-local confirmation authority state.

The confirmation state is the single synchronization domain for human-confirmation
runtime authority. It owns the process-local epoch, pending token store, monotonic
authority clock, and final consume-time validation. Wall-clock timestamps remain
diagnostic only.

Source of truth: ``docs/M6_DESIGN.md`` §11.1 and acceptance R14/R15/R40/R47/R48.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from typing import Any, Optional

from webwire.safety.models import ConfirmationToken, RiskTier

__all__ = ["ConfirmationState"]


class ConfirmationState:
    """Own synchronized, process-local confirmation authority.

    All authority-sensitive state and clock sampling is enclosed by one re-entrant
    fence. ``advance_epoch`` is deliberately global within this state: advancing it
    invalidates every token minted under an older epoch, including tokens unrelated
    to the reconciliation that caused the advance.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = 300.0,
        monotonic_clock: Optional[Callable[[], float]] = None,
        wall_clock: Optional[Callable[[], float]] = None,
        token_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = float(ttl_seconds)
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(16))
        self._lock = threading.RLock()
        self._epoch = 0
        self._pending: dict[str, ConfirmationToken] = {}

    @property
    def current_epoch(self) -> int:
        """Return the current confirmation epoch under the confirmation fence."""
        with self._lock:
            return self._epoch

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def advance_epoch(self) -> int:
        """Synchronously revoke every token issued under an older epoch."""
        with self._lock:
            self._epoch += 1
            return self._epoch

    def issue(
        self,
        *,
        intent_hash: str,
        risk_tier: RiskTier,
        capability_name: str,
    ) -> ConfirmationToken:
        """Mint and store one token bound to the current epoch and monotonic TTL."""
        if not intent_hash:
            raise ValueError("intent_hash must be non-empty")
        if not capability_name:
            raise ValueError("capability_name must be non-empty")
        if not isinstance(risk_tier, RiskTier):
            raise TypeError("risk_tier must be a RiskTier")

        with self._lock:
            authority_now = float(self._monotonic_clock())
            wall_now = float(self._wall_clock())
            token_str = self._token_factory()
            if not isinstance(token_str, str) or not token_str:
                raise ValueError("token_factory must return a non-empty string")
            if token_str in self._pending:
                raise RuntimeError("token_factory produced a duplicate confirmation token")

            token = ConfirmationToken(
                token=token_str,
                intent_hash=intent_hash,
                risk_tier=risk_tier,
                created_at=wall_now,
                expires_at=wall_now + self._ttl_seconds,
                capability_name=capability_name,
                confirmation_epoch=self._epoch,
                authority_created_at=authority_now,
                authority_expires_at=authority_now + self._ttl_seconds,
            )
            self._pending[token_str] = token
            return token

    def validate_and_consume(
        self,
        token_str: Any,
        *,
        intent_hash: str,
        risk_tier: RiskTier,
        capability_name: str,
    ) -> tuple[Optional[ConfirmationToken], Optional[str]]:
        """Atomically validate and consume one token.

        The monotonic clock is sampled while holding the same fence that protects
        epoch comparison, token lookup, validation, and the consumed mutation. This
        sample is therefore the actual authority consume boundary, not an earlier
        preview/policy timestamp.

        Returns ``(token, None)`` on success or ``(token-or-None, blocked_by)`` on
        denial. Unknown tokens deliberately retain the historical
        ``consumed_token`` denial code.
        """
        with self._lock:
            token = self._pending.get(token_str) if isinstance(token_str, str) else None
            authority_now = float(self._monotonic_clock())

            if token is None:
                return None, "consumed_token"
            if token.confirmation_epoch != self._epoch:
                return token, "stale_confirmation_epoch"
            if token.is_expired(authority_now):
                return token, "expired_token"
            if token.consumed:
                return token, "consumed_token"
            if token.capability_name != capability_name:
                return token, "capability_mismatch"
            if token.intent_hash != intent_hash:
                return token, "intent_mismatch"
            if token.risk_tier != risk_tier:
                return token, "intent_mismatch"

            token.consumed = True
            return token, None

    def diagnostic_token(self, token_str: str) -> Optional[ConfirmationToken]:
        """Return a pending token object for diagnostics/tests, never authority."""
        with self._lock:
            return self._pending.get(token_str)
