"""M6 Layer-4 reconciliation must preserve independent protocol/gate semantics."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationCoordinator,
    ReconciliationDenied,
    ReconciliationLedger,
    ReconciliationVerdict,
    RecoveryGuard,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch


def _evidence() -> dict[str, object]:
    return {
        "basis": "operator-review",
        "observed_at": "2026-09-26T20:01:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def test_kill_may_remain_tripped_while_local_reconciliation_completes(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    raw = EffectLedgerRecord(
        effect_id="fx-kill",
        semantic_key="actor|like|post|kill|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-kill",
        policy_binding="policy-kill",
        actor_id="actor",
        target_type="post",
        target_id="kill",
        timestamp="2026-09-26T20:00:00+00:00",
    )
    effects.append_durable(raw)

    kill = KillSwitch(cfg)
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=kill,
        authorization_epoch=AuthorizationEpoch(),
    )
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    guard.hydrate()
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=ConfirmationState(),
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: "rec-kill",
        timestamp_factory=lambda: "2026-09-26T20:02:00+00:00",
    )
    evidence = _evidence()
    authority = coordinator._mint_operator_authority(
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-kill",
    )

    kill.trip()
    assert kill.tripped() is True

    result = coordinator.resolve(
        raw.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence,
        authority,
    )

    assert result.record.effect_id == raw.effect_id
    assert authority.consumed is True
    assert guard.require_clear(raw.semantic_key, refresh=False) is None
    assert kill.tripped() is True


def test_reconciliation_id_collision_denies_before_epoch_or_authority_commit(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    first = EffectLedgerRecord(
        effect_id="fx-first",
        semantic_key="actor|like|post|first|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-first",
        policy_binding="policy-binding",
        actor_id="actor",
        target_type="post",
        target_id="first",
        timestamp="2026-09-26T20:10:00+00:00",
    )
    second = EffectLedgerRecord(
        effect_id="fx-second",
        semantic_key="actor|like|post|second|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-second",
        policy_binding="policy-binding",
        actor_id="actor",
        target_type="post",
        target_id="second",
        timestamp="2026-09-26T20:10:01+00:00",
    )
    effects.append_durable(first)
    effects.append_durable(second)

    confirmations = ConfirmationState()
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    guard.hydrate()
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: "rec-collision",
        timestamp_factory=lambda: "2026-09-26T20:11:00+00:00",
    )
    evidence = _evidence()

    first_authority = coordinator._mint_operator_authority(
        effect_id=first.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-first",
    )
    coordinator.resolve(
        first.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence,
        first_authority,
    )
    assert confirmations.current_epoch == 1

    second_authority = coordinator._mint_operator_authority(
        effect_id=second.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-second",
    )
    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            second.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            second_authority,
        )

    assert exc_info.value.reason == "reconciliation_id_collision"
    assert confirmations.current_epoch == 1
    assert second_authority.committed is False
    records = reconciliations.read_authoritative()
    assert len(records) == 1
    assert records[0].effect_id == first.effect_id
