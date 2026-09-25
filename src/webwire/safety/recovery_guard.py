"""Layer-6 replay denial from durable M5 recovery truth.

``RecoveryGuard`` turns the canonical :class:`EffectLedger` recovery projection
into an enforced semantic replay gate. It never reconciles or rewrites ledger
history: unresolved ``RESERVED`` and ``EFFECT_UNKNOWN`` facts remain durable
uncertainty until a future evidence-bearing reconciliation design resolves them.

The guard is hydrated at Dispatcher startup and refreshed before every supported
M5 mutation policy pass. Refresh is intentional: an effect can become durably
unknown while the current process survives, so restart-only hydration would
leave a same-process replay window.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerError,
    EffectState,
    RecoveryProjection,
)

__all__ = [
    "RecoveryBlock",
    "RecoveryEffect",
    "RecoveryGuard",
    "RecoveryGuardUnavailable",
    "RecoveryStatus",
]


class RecoveryGuardUnavailable(RuntimeError):
    """Recovery authority could not be established from the EffectLedger."""


@dataclass(frozen=True)
class RecoveryEffect:
    """One unresolved durable effect represented in a semantic replay block."""

    effect_id: str
    raw_state: EffectState
    effective_state: EffectState


@dataclass(frozen=True)
class RecoveryBlock:
    """All unresolved durable effects sharing one exact semantic key."""

    semantic_key: str
    effects: tuple[RecoveryEffect, ...]

    @property
    def effect_ids(self) -> tuple[str, ...]:
        return tuple(effect.effect_id for effect in self.effects)


@dataclass(frozen=True)
class RecoveryStatus:
    """Diagnostic guard state. Enforcement remains in ``require_clear``."""

    hydrated: bool
    available: bool
    unresolved_semantic_keys: tuple[str, ...]
    unresolved_effect_count: int
    error: Optional[str] = None


class RecoveryGuard:
    """Deny exact semantic replay while durable uncertainty exists.

    A guard instance is process-local, but its authority comes only from the
    fsync-backed EffectLedger. Any ledger read/corruption failure makes the guard
    unavailable and therefore fail-closed; stale cached state is never treated
    as proof that a semantic key is clear.
    """

    def __init__(self, ledger: EffectLedger) -> None:
        if not isinstance(ledger, EffectLedger):
            raise TypeError("RecoveryGuard requires an EffectLedger")
        self._ledger = ledger
        self._lock = threading.RLock()
        self._blocks: dict[str, RecoveryBlock] = {}
        self._hydrated = False
        self._available = False
        self._error: Optional[str] = None

    @property
    def ledger(self) -> EffectLedger:
        return self._ledger

    def hydrate(self) -> RecoveryStatus:
        """Establish startup recovery truth from the canonical ledger."""
        return self.refresh()

    def refresh(self) -> RecoveryStatus:
        """Replace the cache from one validated ledger recovery projection.

        The full read -> projection -> publication sequence is serialized under
        the guard lock. Without that ordering, two concurrent refreshes could
        publish snapshots out of order and let an older clear view overwrite a
        newer unresolved one.

        The ledger validates syntax, lineage, and state history before returning
        a projection. If that read fails, cached blocks are discarded as
        authority and the guard remains unavailable until a later refresh can
        establish a fresh trustworthy projection.
        """
        with self._lock:
            try:
                projection = self._ledger.recovery_projection()
            except EffectLedgerError as exc:
                self._blocks = {}
                self._hydrated = True
                self._available = False
                self._error = f"{type(exc).__name__}: {exc}"
                raise RecoveryGuardUnavailable(
                    "M5 recovery state could not be established from the EffectLedger"
                ) from exc

            self._blocks = self._blocks_from_projection(projection)
            self._hydrated = True
            self._available = True
            self._error = None
            return self._status_locked()

    def require_clear(
        self,
        semantic_key: str,
        *,
        refresh: bool = True,
    ) -> Optional[RecoveryBlock]:
        """Return a replay block for ``semantic_key`` or ``None`` when clear.

        By default every check refreshes from durable truth before answering.
        Callers may disable refresh only when they already hold a fresh startup
        snapshot and no mutation can have appended a newer ledger fact.
        """
        if not isinstance(semantic_key, str) or not semantic_key:
            raise ValueError("semantic_key must be a non-empty string")
        if refresh:
            self.refresh()
        with self._lock:
            if not self._hydrated or not self._available:
                raise RecoveryGuardUnavailable(
                    "M5 recovery state is unavailable; mutation must fail closed"
                )
            return self._blocks.get(semantic_key)

    def status(self) -> RecoveryStatus:
        """Return diagnostic cached state without refreshing or enforcing."""
        with self._lock:
            return self._status_locked()

    def _status_locked(self) -> RecoveryStatus:
        keys = tuple(sorted(self._blocks)) if self._available else ()
        count = (
            sum(len(block.effects) for block in self._blocks.values())
            if self._available
            else 0
        )
        return RecoveryStatus(
            hydrated=self._hydrated,
            available=self._available,
            unresolved_semantic_keys=keys,
            unresolved_effect_count=count,
            error=self._error,
        )

    @staticmethod
    def _blocks_from_projection(
        projection: list[RecoveryProjection],
    ) -> dict[str, RecoveryBlock]:
        grouped: dict[str, list[RecoveryEffect]] = {}
        for item in projection:
            if not item.unresolved:
                continue
            grouped.setdefault(item.semantic_key, []).append(
                RecoveryEffect(
                    effect_id=item.effect_id,
                    raw_state=item.raw_state,
                    effective_state=item.effective_state,
                )
            )

        blocks: dict[str, RecoveryBlock] = {}
        for semantic_key, effects in grouped.items():
            ordered = tuple(sorted(effects, key=lambda effect: effect.effect_id))
            blocks[semantic_key] = RecoveryBlock(
                semantic_key=semantic_key,
                effects=ordered,
            )
        return blocks
