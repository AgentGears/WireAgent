"""M6 process-local confirmation authority state.

The confirmation state is the single synchronization domain for human-confirmation
runtime authority. It owns the process-local epoch, pending token store, monotonic
authority clock, and final consume-time validation. Wall-clock timestamps and the
returned token object are diagnostic/carrier surfaces only; canonical authority is
held privately by this state.

Source of truth: ``docs/M6_DESIGN.md`` §11.1 and acceptance R14/R15/R40/R47/R48.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from webwire.safety.models import ConfirmationToken, RiskTier

__all__ = ["ConfirmationState", "ConfirmationStateError"]


class ConfirmationStateError(RuntimeError):
    """Confirmation authority could not be sampled or maintained safely."""


@dataclass
class _PendingAuthority:
    """Canonical private authority; never reconstructed from mutable token fields."""

    token: ConfirmationToken
    intent_hash: str
    risk_tier: RiskTier
    capability_name: str
    confirmation_epoch: int
    authority_expires_at: float
    consumed: bool = False


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
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("ttl_seconds must be a finite positive number") from exc
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")

        self._ttl_seconds = ttl
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._wall_clock = wall_clock or time.time
        self._token_factory = token_factory or (lambda: secrets.token_urlsafe(16))
        self._lock = threading.RLock()
        self._epoch = 0
        self._pending: dict[str, _PendingAuthority] = {}
        self._last_authority_sample: Optional[float] = None

    @property
    def current_epoch(self) -> int:
        """Return the current confirmation epoch under the confirmation fence."""
        with self._lock:
            return self._epoch

    @property
    def ttl_seconds(self) -> float:
        return self._ttl_seconds

    def _sample_authority_locked(self) -> float:
        """Sample a finite, non-regressing monotonic authority clock under lock."""
        try:
            value = float(self._monotonic_clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConfirmationStateError(
                "confirmation authority clock returned an invalid value"
            ) from exc
        if not math.isfinite(value):
            raise ConfirmationStateError(
                "confirmation authority clock returned a non-finite value"
            )
        previous = self._last_authority_sample
        if previous is not None and value < previous:
            raise ConfirmationStateError("confirmation authority clock regressed")
        self._last_authority_sample = value
        return value

    def _sample_wall_locked(self) -> float:
        """Sample finite wall-clock provenance; wall-clock ordering is not authority."""
        try:
            value = float(self._wall_clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ConfirmationStateError(
                "confirmation wall clock returned an invalid value"
            ) from exc
        if not math.isfinite(value):
            raise ConfirmationStateError(
                "confirmation wall clock returned a non-finite value"
            )
        return value

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
            authority_now = self._sample_authority_locked()
            wall_now = self._sample_wall_locked()
            token_str = self._token_factory()
            if not isinstance(token_str, str) or not token_str:
                raise ValueError("token_factory must return a non-empty string")
            if token_str in self._pending:
                raise ConfirmationStateError(
                    "token_factory produced a duplicate confirmation token"
                )

            authority_expires_at = authority_now + self._ttl_seconds
            if not math.isfinite(authority_expires_at):
                raise ConfirmationStateError(
                    "confirmation authority deadline is non-finite"
                )
            wall_expires_at = wall_now + self._ttl_seconds
            if not math.isfinite(wall_expires_at):
                raise ConfirmationStateError(
                    "confirmation wall-clock diagnostic deadline is non-finite"
                )

            token = ConfirmationToken(
                token=token_str,
                intent_hash=intent_hash,
                risk_tier=risk_tier,
                created_at=wall_now,
                expires_at=wall_expires_at,
                capability_name=capability_name,
                confirmation_epoch=self._epoch,
                authority_created_at=authority_now,
                authority_expires_at=authority_expires_at,
            )
            self._pending[token_str] = _PendingAuthority(
                token=token,
                intent_hash=intent_hash,
                risk_tier=risk_tier,
                capability_name=capability_name,
                confirmation_epoch=self._epoch,
                authority_expires_at=authority_expires_at,
            )
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

        Canonical comparison uses only the private ``_PendingAuthority`` record;
        mutating the returned ``ConfirmationToken`` object cannot extend lifetime,
        change epoch/bindings, or resurrect consumed authority.
        """
        with self._lock:
            pending = self._pending.get(token_str) if isinstance(token_str, str) else None
            authority_now = self._sample_authority_locked()

            if pending is None:
                return None, "consumed_token"
            token = pending.token
            if pending.confirmation_epoch != self._epoch:
                return token, "stale_confirmation_epoch"
            if authority_now >= pending.authority_expires_at:
                return token, "expired_token"
            if pending.consumed:
                return token, "consumed_token"
            if pending.capability_name != capability_name:
                return token, "capability_mismatch"
            if pending.intent_hash != intent_hash:
                return token, "intent_mismatch"
            if pending.risk_tier != risk_tier:
                return token, "intent_mismatch"

            pending.consumed = True
            token.consumed = True  # compatibility/diagnostic mirror; not authority
            return token, None

    def _diagnostic_pending_tokens(self) -> dict[str, ConfirmationToken]:
        """Return a snapshot for legacy tests/diagnostics; never mutation authority."""
        with self._lock:
            return {key: pending.token for key, pending in self._pending.items()}
