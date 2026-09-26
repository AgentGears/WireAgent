"""M6 terminal reconciliation protocol coordinator.

The coordinator composes immutable M5 effect history, ephemeral operator
ReconciliationAuthority, confirmation-generation invalidation, the durable
ReconciliationLedger, and RecoveryGuard publication under one process-local
ordering.

Source of truth: ``docs/M6_DESIGN.md`` §§10-16 and acceptance R3-R45.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.confirmation_state import ConfirmationState
from webwire.safety.effect_ledger import (
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.reconciliation_authority import (
    ReconciliationAuthority,
    ReconciliationAuthorityError,
)
from webwire.safety.reconciliation_ledger import (
    ReconciliationLedgerAmbiguousError,
    ReconciliationLedgerError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)
from webwire.safety.recovery_guard import (
    RecoveryGuard,
    RecoveryGuardUnavailable,
    RecoveryStatus,
)
from webwire.safety.recovery_projector import CompositeRecoveryProjection

__all__ = [
    "ReconciliationCoordinator",
    "ReconciliationCoordinatorError",
    "ReconciliationDenied",
    "ReconciliationPersistenceError",
    "ReconciliationPublicationError",
    "ReconciliationResolution",
    "ReconciliationTarget",
]

_RECONCILABLE_STATES = frozenset({EffectState.RESERVED, EffectState.EFFECT_UNKNOWN})
_LINEAGE_FIELDS = (
    "semantic_key",
    "action_type",
    "intent_hash",
    "policy_binding",
    "actor_id",
    "target_type",
    "target_id",
)


class ReconciliationCoordinatorError(RuntimeError):
    """The local reconciliation protocol could not complete safely."""


class ReconciliationDenied(ReconciliationCoordinatorError):
    """A requested resolution is invalid before persistence begins."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"reconciliation denied: {reason}" + (f" — {detail}" if detail else "")
        )


class ReconciliationPersistenceError(ReconciliationCoordinatorError):
    """Persistence of an already-committed frozen fact did not complete."""

    def __init__(
        self,
        record: ReconciliationRecord,
        *,
        ambiguous: bool,
        detail: str,
    ) -> None:
        self.record = record
        self.ambiguous = ambiguous
        super().__init__(
            "reconciliation persistence failed"
            + (" ambiguously" if ambiguous else " cleanly")
            + f": {detail}"
        )


class ReconciliationPublicationError(ReconciliationCoordinatorError):
    """Fact is durable/consumed but composite guard publication failed closed."""

    def __init__(self, record: ReconciliationRecord, detail: str) -> None:
        self.record = record
        super().__init__(f"durable reconciliation guard publication failed: {detail}")


@dataclass(frozen=True)
class ReconciliationTarget:
    """Read-only current target description for local operator workflow."""

    effect_id: str
    first_record: EffectLedgerRecord
    last_record: EffectLedgerRecord
    projection: CompositeRecoveryProjection


@dataclass(frozen=True)
class ReconciliationResolution:
    """Known-durable resolution and the confirmation generation it invalidated."""

    record: ReconciliationRecord
    confirmation_epoch: int
    recovery_status: RecoveryStatus


class ReconciliationCoordinator:
    """Serialize one M6 terminal reconciliation protocol at a time.

    Lock order is deliberately:

        publication fence
          -> coordinator protocol lock
            -> canonical CommitGateway lifecycle fence
              -> ConfirmationState fence (inside advance_epoch)
              -> ledger/path locks
              -> RecoveryGuard publication/cache lock

    A coordinator cannot be constructed without the CommitGateway that owns the
    M5 lifecycle domain for this EffectLedger. CommitGateway shares that lifecycle
    domain by normalized ledger path, so a sibling gateway in the same process
    cannot hide a live owner. Independent processes remain outside M6's claim.

    The gateway lifecycle fence is held for the whole resolution protocol. Once
    it reports no owner, the target effect cannot later resume an old in-process
    M5 attempt before reconciliation publication completes.

    ReconciliationAuthority lifecycle transitions are keyed to this coordinator.
    Only the private operator-confirmation mint path creates an authority that
    this coordinator will accept; a separately constructed authority cannot
    bypass the explicit local operator workflow through ``resolve()``.
    """

    def __init__(
        self,
        *,
        recovery_guard: RecoveryGuard,
        confirmation_state: ConfirmationState,
        commit_gateway: CommitGateway,
        reconciliation_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16),
        timestamp_factory: Callable[[], str] = lambda: datetime.now(
            timezone.utc
        ).isoformat(),
    ) -> None:
        if not isinstance(recovery_guard, RecoveryGuard):
            raise TypeError("recovery_guard must be a RecoveryGuard")
        if not isinstance(confirmation_state, ConfirmationState):
            raise TypeError("confirmation_state must be a ConfirmationState")
        if not isinstance(commit_gateway, CommitGateway):
            raise TypeError("commit_gateway must be the canonical CommitGateway")
        if (
            commit_gateway.ledger.path.resolve(strict=False)
            != recovery_guard.ledger.path.resolve(strict=False)
        ):
            raise ValueError("commit_gateway and recovery_guard EffectLedger paths differ")

        self._guard = recovery_guard
        self._effect_ledger = recovery_guard.ledger
        self._reconciliation_ledger = recovery_guard.reconciliation_ledger
        self._publication_fence = recovery_guard.publication_fence
        self._confirmation_state = confirmation_state
        self._gateway = commit_gateway
        self._reconciliation_id_factory = reconciliation_id_factory
        self._timestamp_factory = timestamp_factory
        self._protocol_lock = threading.RLock()
        self.__authority_protocol_key = object()

    @property
    def recovery_guard(self) -> RecoveryGuard:
        return self._guard

    @property
    def confirmation_state(self) -> ConfirmationState:
        return self._confirmation_state

    @property
    def commit_gateway(self) -> CommitGateway:
        return self._gateway

    def _mint_operator_authority(
        self,
        *,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence_hash: str,
        operator_id: str,
        ttl_seconds: float,
        monotonic_clock: Callable[[], float],
        authority_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> ReconciliationAuthority:
        """Mint a coordinator-bound authority after operator confirmation.

        This is intentionally private. ReconciliationOperatorSession is the
        supported caller and invokes it only after exact same-session human
        confirmation of the frozen proposal.
        """
        return ReconciliationAuthority(
            effect_id=effect_id,
            verdict=verdict,
            evidence_hash=evidence_hash,
            operator_id=operator_id,
            _protocol_key=self.__authority_protocol_key,
            ttl_seconds=ttl_seconds,
            monotonic_clock=monotonic_clock,
            authority_id_factory=authority_id_factory,
        )

    def list_targets(self) -> tuple[ReconciliationTarget, ...]:
        """Return read-only unresolved composite targets for operator display."""
        projection = self._guard.projector.project()
        return tuple(
            ReconciliationTarget(
                effect_id=item.effect_id,
                first_record=item.first_record,
                last_record=item.last_record,
                projection=item,
            )
            for item in projection
            if item.unresolved
        )

    def describe_target(self, effect_id: str) -> ReconciliationTarget:
        """Return one displayable target, denying any current live owner.

        Operator confirmation must not be staged against evidence that predates
        completion of the same in-process M5 attempt. Use the same lock order as
        terminal resolution so a live owner cannot disappear/reappear across the
        target snapshot.
        """
        if not isinstance(effect_id, str) or not effect_id:
            raise ReconciliationDenied("invalid_effect_id")
        with self._publication_fence:
            with self._protocol_lock:
                with self._gateway.reconciliation_lifecycle_fence():
                    if self._gateway.live_attempt_owns_effect(effect_id):
                        raise ReconciliationDenied("live_attempt_owned", effect_id)
                    projection = self._guard.projector.project()
                    for item in projection:
                        if item.effect_id != effect_id:
                            continue
                        if not item.unresolved:
                            raise ReconciliationDenied(
                                "invalid_target_state",
                                item.disposition.value,
                            )
                        return ReconciliationTarget(
                            effect_id=item.effect_id,
                            first_record=item.first_record,
                            last_record=item.last_record,
                            projection=item,
                        )
        raise ReconciliationDenied("unknown_effect_id", effect_id)

    @staticmethod
    def _history_target(
        records: list[EffectLedgerRecord],
        effect_id: str,
    ) -> tuple[EffectLedgerRecord, EffectLedgerRecord]:
        first: Optional[EffectLedgerRecord] = None
        last: Optional[EffectLedgerRecord] = None
        for record in records:
            if record.effect_id != effect_id:
                continue
            if first is None:
                first = record
            last = record
        if first is None or last is None:
            raise ReconciliationDenied("unknown_effect_id", effect_id)
        if last.state not in _RECONCILABLE_STATES:
            raise ReconciliationDenied("invalid_target_state", last.state.value)
        return first, last

    @staticmethod
    def _require_frozen_lineage(
        *,
        first: EffectLedgerRecord,
        record: ReconciliationRecord,
    ) -> None:
        if record.effect_id != first.effect_id:
            raise ReconciliationDenied("committed_fact_mismatch", "effect_id")
        for field_name in _LINEAGE_FIELDS:
            if getattr(first, field_name) != getattr(record, field_name):
                raise ReconciliationDenied(
                    "committed_fact_mismatch",
                    f"canonical lineage field {field_name} changed",
                )

    def _new_record(
        self,
        *,
        first: EffectLedgerRecord,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence_hash: str,
        evidence: dict[str, Any],
        authority: ReconciliationAuthority,
    ) -> ReconciliationRecord:
        reconciliation_id = self._reconciliation_id_factory()
        if not isinstance(reconciliation_id, str) or not reconciliation_id:
            raise ReconciliationCoordinatorError(
                "reconciliation_id_factory returned an invalid identifier"
            )
        timestamp = self._timestamp_factory()
        if not isinstance(timestamp, str) or not timestamp:
            raise ReconciliationCoordinatorError(
                "timestamp_factory returned an invalid timestamp"
            )
        try:
            return ReconciliationRecord(
                reconciliation_id=reconciliation_id,
                effect_id=effect_id,
                semantic_key=first.semantic_key,
                action_type=first.action_type,
                intent_hash=first.intent_hash,
                policy_binding=first.policy_binding,
                actor_id=first.actor_id,
                target_type=first.target_type,
                target_id=first.target_id,
                verdict=verdict,
                operator_id=authority.operator_id,
                evidence_hash=evidence_hash,
                evidence=evidence,
                timestamp=timestamp,
            )
        except ValueError as exc:
            raise ReconciliationDenied("invalid_reconciliation_record", str(exc)) from exc

    def resolve(
        self,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence: dict[str, Any],
        operator_authority: ReconciliationAuthority,
    ) -> ReconciliationResolution:
        """Resolve one unresolved effect using the frozen M6 ordering protocol."""
        if not isinstance(effect_id, str) or not effect_id:
            raise ReconciliationDenied("invalid_effect_id")
        if not isinstance(verdict, ReconciliationVerdict):
            raise ReconciliationDenied("invalid_verdict")
        if not isinstance(operator_authority, ReconciliationAuthority):
            raise ReconciliationDenied("operator_authority_missing")

        with self._publication_fence:
            with self._protocol_lock:
                with self._gateway.reconciliation_lifecycle_fence():
                    if self._gateway.live_attempt_owns_effect(effect_id):
                        raise ReconciliationDenied("live_attempt_owned", effect_id)

                    try:
                        effect_records = self._effect_ledger.read_records()
                    except EffectLedgerError as exc:
                        raise ReconciliationDenied(
                            "effect_history_unavailable",
                            str(exc),
                        ) from exc
                    first, _last = self._history_target(effect_records, effect_id)

                    try:
                        evidence_hash = canonical_evidence_hash(evidence)
                    except (TypeError, ValueError) as exc:
                        raise ReconciliationDenied("invalid_evidence", str(exc)) from exc

                    # Fresh starts and known-clean committed retries establish a
                    # complete authoritative reconciliation history before any
                    # authority use. A locally ambiguous committed retry is the
                    # one exception: the ledger intentionally blocks ordinary
                    # reads until exact-fact re-durability succeeds.
                    skip_history_read = (
                        operator_authority.committed
                        and self._reconciliation_ledger.durability_ambiguous
                    )
                    existing: Optional[list[ReconciliationRecord]] = None
                    if not skip_history_read:
                        try:
                            existing = self._reconciliation_ledger.read_authoritative()
                        except ReconciliationLedgerError as exc:
                            raise ReconciliationDenied(
                                "reconciliation_history_unavailable",
                                str(exc),
                            ) from exc
                        if any(record.effect_id == effect_id for record in existing):
                            raise ReconciliationDenied("already_reconciled", effect_id)

                    try:
                        committed_record = operator_authority._validate_start(
                            protocol_key=self.__authority_protocol_key,
                            effect_id=effect_id,
                            verdict=verdict,
                            evidence_hash=evidence_hash,
                        )
                    except ReconciliationAuthorityError as exc:
                        raise ReconciliationDenied(exc.reason, exc.detail) from exc

                    if committed_record is None:
                        record = self._new_record(
                            first=first,
                            effect_id=effect_id,
                            verdict=verdict,
                            evidence_hash=evidence_hash,
                            evidence=evidence,
                            authority=operator_authority,
                        )
                    else:
                        record = committed_record
                        self._require_frozen_lineage(first=first, record=record)

                    # A random/id-factory collision is a known invalid proposal,
                    # not a persistence event. Detect it while the authoritative
                    # history is already in hand, before invalidating confirmations
                    # or committing the authority to an impossible retry.
                    if existing is not None and any(
                        persisted.reconciliation_id == record.reconciliation_id
                        for persisted in existing
                    ):
                        raise ReconciliationDenied(
                            "reconciliation_id_collision",
                            record.reconciliation_id,
                        )

                    confirmation_epoch = self._confirmation_state.advance_epoch()

                    try:
                        record = operator_authority._commit_for_persistence(
                            record,
                            protocol_key=self.__authority_protocol_key,
                        )
                    except ReconciliationAuthorityError as exc:
                        raise ReconciliationCoordinatorError(
                            f"authority commitment failed after epoch advance: {exc}"
                        ) from exc

                    try:
                        self._reconciliation_ledger.append_durable(record)
                    except ReconciliationLedgerError as exc:
                        raise ReconciliationPersistenceError(
                            record,
                            ambiguous=(
                                isinstance(exc, ReconciliationLedgerAmbiguousError)
                                or self._reconciliation_ledger.durability_ambiguous
                            ),
                            detail=str(exc),
                        ) from exc

                    try:
                        operator_authority._consume_after_durable(
                            record,
                            protocol_key=self.__authority_protocol_key,
                        )
                    except ReconciliationAuthorityError as exc:
                        raise ReconciliationCoordinatorError(
                            f"durable fact could not consume operator authority: {exc}"
                        ) from exc

                    try:
                        status = self._guard.refresh()
                    except RecoveryGuardUnavailable as exc:
                        raise ReconciliationPublicationError(record, str(exc)) from exc

                    return ReconciliationResolution(
                        record=record,
                        confirmation_epoch=confirmation_epoch,
                        recovery_status=status,
                    )
