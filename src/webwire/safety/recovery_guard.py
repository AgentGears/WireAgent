"""M6 replay denial from joined durable effect + reconciliation truth.

``RecoveryGuard`` remains the enforced semantic replay gate used by WriteKernel,
but its authority is now a complete composite snapshot from both safety ledgers.
M5 ``EffectLedger.recovery_projection()`` remains a diagnostic/helper API only.

Authoritative refresh is ordered by ``ReconciliationPublicationFence``. Ledger
reads and composite validation occur before the guard publication/cache lock is
acquired, preserving the frozen M6 lock order:

    publication fence -> ledger/path locks -> guard publication/cache lock

No source history is rewritten. A terminal reconciliation can remove only the
matching unresolved recovery contribution after the reconciliation row is known
durable and lineage-valid.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Optional

from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.reconciliation_ledger import ReconciliationLedger
from webwire.safety.recovery_projector import (
    CompositeRecoveryProjection,
    ReconciliationPublicationFence,
    RecoveryProjector,
)

__all__ = [
    "RecoveryBlock",
    "RecoveryEffect",
    "RecoveryGuard",
    "RecoveryGuardUnavailable",
    "RecoveryStatus",
]


class RecoveryGuardUnavailable(RuntimeError):
    """Composite recovery authority could not be established safely."""


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
    """Deny exact semantic replay while composite durable uncertainty exists.

    A guard instance is process-local. Its authority comes only from a complete,
    validated ``RecoveryProjector`` snapshot. Failure or ambiguity in either
    safety ledger discards cached clear authority and makes mutation fail closed.
    """

    def __init__(
        self,
        ledger: EffectLedger,
        *,
        reconciliation_ledger: Optional[ReconciliationLedger] = None,
    ) -> None:
        if not isinstance(ledger, EffectLedger):
            raise TypeError("RecoveryGuard requires an EffectLedger")
        sibling_reconciliation = (
            reconciliation_ledger
            if reconciliation_ledger is not None
            else ReconciliationLedger(path=ledger.path.parent / "reconciliations.ndjson")
        )
        if not isinstance(sibling_reconciliation, ReconciliationLedger):
            raise TypeError("reconciliation_ledger must be a ReconciliationLedger")

        self._ledger = ledger
        self._reconciliation_ledger = sibling_reconciliation
        self._projector = RecoveryProjector(ledger, sibling_reconciliation)
        self._publication_fence = self._projector.publication_fence
        self._lock = threading.RLock()
        self._blocks: dict[str, RecoveryBlock] = {}
        self._hydrated = False
        self._available = False
        self._error: Optional[str] = None

    @property
    def ledger(self) -> EffectLedger:
        """Canonical M5 effect ledger retained for compatibility/diagnostics."""
        return self._ledger

    @property
    def reconciliation_ledger(self) -> ReconciliationLedger:
        return self._reconciliation_ledger

    @property
    def projector(self) -> RecoveryProjector:
        return self._projector

    @property
    def publication_fence(self) -> ReconciliationPublicationFence:
        return self._publication_fence

    def hydrate(self) -> RecoveryStatus:
        """Establish startup recovery truth from both canonical safety ledgers."""
        return self.refresh()

    def refresh(self) -> RecoveryStatus:
        """Publish one complete, publication-fenced composite recovery snapshot.

        The publication fence spans both durable reads, composite validation,
        and cache publication. Crucially, the guard cache lock is acquired only
        *after* ledger/path locks have been released by the projector. This
        preserves the M6 lock order needed by the later reconciliation
        coordinator and prevents an older clear snapshot from overtaking a newer
        blocked/unavailable publication.
        """
        with self._publication_fence:
            try:
                # RecoveryProjector also acquires this same domain fence so a
                # direct projector call cannot bypass ordering. RLock re-entry
                # here is deliberate: this outer hold extends through publication.
                projection = self._projector.project()
                blocks = self._blocks_from_projection(projection)
            except Exception as exc:  # noqa: BLE001 - safety boundary fails closed
                with self._lock:
                    self._blocks = {}
                    self._hydrated = True
                    self._available = False
                    self._error = f"{type(exc).__name__}: {exc}"
                raise RecoveryGuardUnavailable(
                    "M6 composite recovery state could not be established from "
                    "the safety ledgers"
                ) from exc

            with self._lock:
                self._blocks = blocks
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

        By default every check refreshes from composite durable truth before
        answering. Callers may disable refresh only when they already hold a
        fresh authoritative snapshot and no relevant mutation can have advanced
        either safety history.
        """
        if not isinstance(semantic_key, str) or not semantic_key:
            raise ValueError("semantic_key must be a non-empty string")
        if refresh:
            self.refresh()
        with self._lock:
            if not self._hydrated or not self._available:
                raise RecoveryGuardUnavailable(
                    "M6 composite recovery state is unavailable; mutation must "
                    "fail closed"
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
        projection: list[CompositeRecoveryProjection],
    ) -> dict[str, RecoveryBlock]:
        grouped: dict[str, list[RecoveryEffect]] = {}
        for item in projection:
            if not item.unresolved:
                continue
            effective_state = (
                EffectState.EFFECT_UNKNOWN
                if item.raw_state is EffectState.RESERVED
                else item.raw_state
            )
            grouped.setdefault(item.semantic_key, []).append(
                RecoveryEffect(
                    effect_id=item.effect_id,
                    raw_state=item.raw_state,
                    effective_state=effective_state,
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
