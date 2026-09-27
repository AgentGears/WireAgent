"""M6 Layer-4 committed reconciliation continuation regressions."""

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
    ReconciliationLedgerError,
    ReconciliationPersistenceError,
    ReconciliationVerdict,
    RecoveryGuard,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch


def _evidence(label: str) -> dict[str, object]:
    return {
        "basis": label,
        "observed_at": "2026-09-26T21:20:00+00:00",
        "observations": [{"kind": "operator", "value": label}],
    }


def _coordinator(
    cfg: WebWireConfig,
    *,
    confirmation_state: ConfirmationState,
    reconciliation_id: str,
    timestamp: str,
) -> ReconciliationCoordinator:
    effects = EffectLedger(path=cfg.effects_path())
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    return ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmation_state,
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: reconciliation_id,
        timestamp_factory=lambda: timestamp,
    )


def test_same_path_coordinator_rejects_second_confirmation_epoch(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    canonical = ConfirmationState()
    _coordinator(
        cfg,
        confirmation_state=canonical,
        reconciliation_id="rec-canonical",
        timestamp="2026-09-26T21:18:00+00:00",
    )

    with pytest.raises(ValueError, match="share one ConfirmationState"):
        _coordinator(
            cfg,
            confirmation_state=ConfirmationState(),
            reconciliation_id="rec-second-epoch",
            timestamp="2026-09-26T21:18:01+00:00",
        )


def test_clean_failure_pins_exact_fact_across_same_path_coordinators(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    raw = EffectLedgerRecord(
        effect_id="fx-committed-continuation",
        semantic_key="actor|like|post|continuation|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-continuation",
        policy_binding="policy-continuation",
        actor_id="actor",
        target_type="post",
        target_id="continuation",
        timestamp="2026-09-26T21:19:00+00:00",
    )
    effects.append_durable(raw)

    confirmations = ConfirmationState()
    original = _coordinator(
        cfg,
        confirmation_state=confirmations,
        reconciliation_id="rec-original",
        timestamp="2026-09-26T21:21:00+00:00",
    )
    original.recovery_guard.hydrate()

    evidence_a = _evidence("original-evidence")
    authority_a = original._mint_operator_authority(
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence_a),
        operator_id="operator-a",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-original",
    )

    real_append = original.recovery_guard.reconciliation_ledger.append_durable
    calls = 0

    def fail_once(record: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReconciliationLedgerError("injected known-clean failure")
        real_append(record)  # type: ignore[arg-type]

    monkeypatch.setattr(
        original.recovery_guard.reconciliation_ledger,
        "append_durable",
        fail_once,
    )

    with pytest.raises(ReconciliationPersistenceError) as exc_info:
        original.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence_a,
            authority_a,
        )

    frozen = exc_info.value.record
    assert exc_info.value.ambiguous is False
    assert authority_a.committed is True
    assert authority_a.committed_record is frozen
    assert authority_a.consumed is False
    assert confirmations.current_epoch == 1
    assert original.recovery_guard.reconciliation_ledger.read_authoritative() == []

    # Once persistence starts, the operator workflow may not stage a replacement
    # proposal for the same effect while this exact continuation remains live.
    with pytest.raises(ReconciliationDenied) as exc_info:
        original.describe_target(raw.effect_id)
    assert exc_info.value.reason == "committed_resolution_in_progress"

    # A separately constructed same-path coordinator may have its own protocol
    # key, but it must share the one canonical confirmation epoch. Its fresh
    # contradictory authority still cannot replace the already-started fact.
    sibling = _coordinator(
        cfg,
        confirmation_state=confirmations,
        reconciliation_id="rec-competing",
        timestamp="2026-09-26T21:22:00+00:00",
    )
    evidence_b = _evidence("competing-evidence")
    authority_b = sibling._mint_operator_authority(
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence_b),
        operator_id="operator-b",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-competing",
    )

    with pytest.raises(ReconciliationDenied) as exc_info:
        sibling.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_NO_EFFECT,
            evidence_b,
            authority_b,
        )

    assert exc_info.value.reason == "committed_resolution_in_progress"
    assert confirmations.current_epoch == 1
    assert authority_b.committed is False
    assert sibling.recovery_guard.reconciliation_ledger.read_authoritative() == []

    # Only the original committed authority may continue, and it persists the
    # exact frozen identity rather than creating a replacement fact.
    resolution = original.resolve(
        raw.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_a,
        authority_a,
    )

    assert calls == 2
    assert resolution.record is frozen
    assert resolution.record.reconciliation_id == "rec-original"
    assert resolution.record.verdict is ReconciliationVerdict.CONFIRMED_EFFECT
    assert resolution.record.evidence_hash == canonical_evidence_hash(evidence_a)
    assert confirmations.current_epoch == 2
    assert authority_a.consumed is True
    records = original.recovery_guard.reconciliation_ledger.read_authoritative()
    assert records == [resolution.record]
    assert original.recovery_guard.require_clear(raw.semantic_key, refresh=False) is None
