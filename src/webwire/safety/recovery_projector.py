"""M6 composite recovery projection and publication ordering.

M5 effect history remains canonical and immutable. M6 reconciliation history is
orthogonal evidence that may remove only one unresolved recovery contribution.
This module joins both durable histories without rewriting either ledger.

Every :class:`RecoveryProjector` is publication-fenced, including direct calls.
The fence is process-local and shared by normalized safety-ledger path pair so
independently constructed projectors/guards for one recovery domain cannot use
accidentally independent locks.

Source of truth: ``docs/M6_DESIGN.md`` §§11, 13-15.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Optional

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.reconciliation_ledger import (
    ReconciliationLedger,
    ReconciliationRecord,
    ReconciliationVerdict,
)

__all__ = [
    "CompositeRecoveryProjection",
    "ReconciliationPublicationFence",
    "RecoveryDisposition",
    "RecoveryProjector",
    "RecoveryProjectorCorruptError",
    "RecoveryProjectorError",
]


class RecoveryProjectorError(RuntimeError):
    """Composite recovery authority could not be derived safely."""


class RecoveryProjectorCorruptError(RecoveryProjectorError):
    """The two safety histories cannot form one valid composite history."""


class RecoveryDisposition(StrEnum):
    """M6 composite recovery dispositions; not successors of ``EffectState``."""

    SETTLED_NO_EFFECT = "SETTLED_NO_EFFECT"
    SETTLED_EFFECT = "SETTLED_EFFECT"
    UNRESOLVED_UNKNOWN = "UNRESOLVED_UNKNOWN"
    RECONCILED_EFFECT = "RECONCILED_EFFECT"
    RECONCILED_NO_EFFECT = "RECONCILED_NO_EFFECT"


@dataclass(frozen=True)
class CompositeRecoveryProjection:
    """One joined M5/M6 recovery fact without mutation of source history."""

    effect_id: str
    semantic_key: str
    raw_state: EffectState
    disposition: RecoveryDisposition
    unresolved: bool
    first_record: EffectLedgerRecord
    last_record: EffectLedgerRecord
    reconciliation: Optional[ReconciliationRecord] = None


class ReconciliationPublicationFence:
    """Process-local ordering fence shared by one pair of safety ledgers.

    Construction always names the two canonical ledgers. The underlying lock is
    selected from a normalized-path registry, so there is no public standalone
    constructor path that can accidentally bypass domain sharing.
    """

    _registry_guard: ClassVar[Any] = threading.Lock()
    _locks: ClassVar[dict[tuple[str, str], Any]] = {}

    def __init__(
        self,
        effect_ledger: EffectLedger,
        reconciliation_ledger: ReconciliationLedger,
    ) -> None:
        if not isinstance(effect_ledger, EffectLedger):
            raise TypeError("effect_ledger must be an EffectLedger")
        if not isinstance(reconciliation_ledger, ReconciliationLedger):
            raise TypeError("reconciliation_ledger must be a ReconciliationLedger")
        key = (
            self._normalize(effect_ledger.path),
            self._normalize(reconciliation_ledger.path),
        )
        with self._registry_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
        self._lock = lock

    @staticmethod
    def _normalize(path: Path) -> str:
        return os.path.normcase(str(path.resolve(strict=False)))

    def __enter__(self) -> "ReconciliationPublicationFence":
        self._lock.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self._lock.release()


_LINEAGE_FIELDS = (
    "semantic_key",
    "action_type",
    "intent_hash",
    "policy_binding",
    "actor_id",
    "target_type",
    "target_id",
)
_RECONCILABLE_STATES = frozenset(
    {
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    }
)


class RecoveryProjector:
    """Join canonical effect and reconciliation histories into M6 recovery truth."""

    def __init__(
        self,
        effect_ledger: EffectLedger,
        reconciliation_ledger: ReconciliationLedger,
    ) -> None:
        if not isinstance(effect_ledger, EffectLedger):
            raise TypeError("RecoveryProjector requires an EffectLedger")
        if not isinstance(reconciliation_ledger, ReconciliationLedger):
            raise TypeError("RecoveryProjector requires a ReconciliationLedger")
        self._effect_ledger = effect_ledger
        self._reconciliation_ledger = reconciliation_ledger
        self._publication_fence = ReconciliationPublicationFence(
            effect_ledger,
            reconciliation_ledger,
        )

    @property
    def effect_ledger(self) -> EffectLedger:
        return self._effect_ledger

    @property
    def reconciliation_ledger(self) -> ReconciliationLedger:
        return self._reconciliation_ledger

    @property
    def publication_fence(self) -> ReconciliationPublicationFence:
        return self._publication_fence

    @staticmethod
    def _validate_reconciliation_lineage(
        *,
        first: EffectLedgerRecord,
        reconciliation: ReconciliationRecord,
    ) -> None:
        for field_name in _LINEAGE_FIELDS:
            canonical = getattr(first, field_name)
            copied = getattr(reconciliation, field_name)
            if copied != canonical:
                raise RecoveryProjectorCorruptError(
                    f"reconciliation for effect_id {reconciliation.effect_id!r} "
                    f"changed {field_name} from canonical {canonical!r} to {copied!r}"
                )

    @staticmethod
    def _disposition(
        *,
        last: EffectLedgerRecord,
        reconciliation: Optional[ReconciliationRecord],
    ) -> RecoveryDisposition:
        if last.state is EffectState.NO_EFFECT:
            if reconciliation is not None:
                raise RecoveryProjectorCorruptError(
                    f"settled NO_EFFECT effect_id {last.effect_id!r} has reconciliation"
                )
            return RecoveryDisposition.SETTLED_NO_EFFECT

        if last.state is EffectState.EFFECT_CONFIRMED:
            if reconciliation is not None:
                raise RecoveryProjectorCorruptError(
                    f"settled EFFECT_CONFIRMED effect_id {last.effect_id!r} has reconciliation"
                )
            return RecoveryDisposition.SETTLED_EFFECT

        if last.state not in _RECONCILABLE_STATES:
            raise RecoveryProjectorCorruptError(
                f"effect_id {last.effect_id!r} has unsupported recovery state "
                f"{last.state.value}"
            )

        if reconciliation is None:
            return RecoveryDisposition.UNRESOLVED_UNKNOWN
        if reconciliation.verdict is ReconciliationVerdict.CONFIRMED_EFFECT:
            return RecoveryDisposition.RECONCILED_EFFECT
        if reconciliation.verdict is ReconciliationVerdict.CONFIRMED_NO_EFFECT:
            return RecoveryDisposition.RECONCILED_NO_EFFECT
        raise RecoveryProjectorCorruptError(
            f"effect_id {last.effect_id!r} has unsupported reconciliation verdict"
        )

    def project(self) -> list[CompositeRecoveryProjection]:
        """Return one publication-fenced complete joined snapshot or fail closed.

        ``ReconciliationLedger.read_authoritative`` is intentionally used rather
        than a raw parse. In the current process it refuses a local durability
        ambiguity latch; after restart it re-establishes surviving file and
        parent-directory durability before any reconciliation can clear recovery.
        """
        with self._publication_fence:
            return self._project_under_fence()

    def _project_under_fence(self) -> list[CompositeRecoveryProjection]:
        effect_records = self._effect_ledger.read_records()
        reconciliation_records = self._reconciliation_ledger.read_authoritative()

        first_by_effect: dict[str, EffectLedgerRecord] = {}
        last_by_effect: dict[str, EffectLedgerRecord] = {}
        for record in effect_records:
            first_by_effect.setdefault(record.effect_id, record)
            last_by_effect[record.effect_id] = record

        reconciliation_by_effect: dict[str, ReconciliationRecord] = {}
        for reconciliation in reconciliation_records:
            first = first_by_effect.get(reconciliation.effect_id)
            last = last_by_effect.get(reconciliation.effect_id)
            if first is None or last is None:
                raise RecoveryProjectorCorruptError(
                    f"reconciliation targets missing effect_id "
                    f"{reconciliation.effect_id!r}"
                )
            if last.state not in _RECONCILABLE_STATES:
                raise RecoveryProjectorCorruptError(
                    f"reconciliation targets already-settled effect_id "
                    f"{reconciliation.effect_id!r} in state {last.state.value}"
                )
            self._validate_reconciliation_lineage(
                first=first,
                reconciliation=reconciliation,
            )
            reconciliation_by_effect[reconciliation.effect_id] = reconciliation

        projected: list[CompositeRecoveryProjection] = []
        for effect_id, last in last_by_effect.items():
            reconciliation = reconciliation_by_effect.get(effect_id)
            disposition = self._disposition(
                last=last,
                reconciliation=reconciliation,
            )
            projected.append(
                CompositeRecoveryProjection(
                    effect_id=effect_id,
                    semantic_key=last.semantic_key,
                    raw_state=last.state,
                    disposition=disposition,
                    unresolved=(
                        disposition is RecoveryDisposition.UNRESOLVED_UNKNOWN
                    ),
                    first_record=first_by_effect[effect_id],
                    last_record=last,
                    reconciliation=reconciliation,
                )
            )

        return sorted(projected, key=lambda item: item.effect_id)
