"""M6 Layer-4 reconciliation coordinator protocol regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationAuthority,
    ReconciliationCoordinator,
    ReconciliationDenied,
    ReconciliationLedger,
    ReconciliationLedgerError,
    ReconciliationPersistenceError,
    ReconciliationVerdict,
    RecoveryGuard,
    RiskTier,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _evidence(label: str = "operator-review") -> dict[str, object]:
    return {
        "basis": label,
        "observed_at": "2026-09-26T18:00:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def _effect(
    *,
    effect_id: str = "fx-1",
    semantic_key: str = "alice|like|post|123|",
    state: EffectState = EffectState.EFFECT_UNKNOWN,
) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=semantic_key,
        state=state,
        action_type="like",
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="alice",
        target_type="post",
        target_id="123",
        timestamp="2026-09-26T17:59:00+00:00",
    )


def _stack(tmp_path: Path) -> tuple[
    EffectLedger,
    ReconciliationLedger,
    RecoveryGuard,
    ConfirmationState,
    ReconciliationCoordinator,
]:
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    confirmations = ConfirmationState()
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: "rec-1",
        timestamp_factory=lambda: "2026-09-26T18:01:00+00:00",
    )
    return effects, reconciliations, guard, confirmations, coordinator


def _authority(
    coordinator: ReconciliationCoordinator,
    evidence: dict[str, object],
    *,
    clock: _Clock | None = None,
    effect_id: str = "fx-1",
    verdict: ReconciliationVerdict = ReconciliationVerdict.CONFIRMED_EFFECT,
) -> ReconciliationAuthority:
    return coordinator._mint_operator_authority(
        effect_id=effect_id,
        verdict=verdict,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="operator-local",
        ttl_seconds=5.0,
        monotonic_clock=clock or _Clock(),
        authority_id_factory=lambda: "auth-1",
    )


def test_resolve_appends_fact_advances_epoch_and_clears_only_recovery_gate(
    tmp_path: Path,
) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _effect()
    effects.append_durable(raw)
    initial = guard.hydrate()
    assert initial.unresolved_effect_count == 1
    assert guard.require_clear(raw.semantic_key, refresh=False) is not None

    evidence = _evidence()
    pending = confirmations.issue(
        intent_hash="unrelated-intent",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark",
    )
    authority = _authority(coordinator, evidence)

    resolution = coordinator.resolve(
        raw.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence,
        authority,
    )

    assert resolution.confirmation_epoch == 1
    assert confirmations.current_epoch == 1
    assert authority.consumed is True
    records = reconciliations.read_authoritative()
    assert len(records) == 1
    record = records[0]
    assert record.reconciliation_id == "rec-1"
    assert record.effect_id == raw.effect_id
    assert record.semantic_key == raw.semantic_key
    assert record.intent_hash == raw.intent_hash
    assert record.policy_binding == raw.policy_binding
    assert record.actor_id == raw.actor_id
    assert record.target_type == raw.target_type
    assert record.target_id == raw.target_id
    assert record.operator_id == "operator-local"
    assert effects.read_records() == [raw]
    assert guard.require_clear(raw.semantic_key, refresh=False) is None

    _, denied_reason = confirmations.validate_and_consume(
        pending.token,
        intent_hash="unrelated-intent",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark",
    )
    assert denied_reason == "stale_confirmation_epoch"


def test_settled_raw_effect_is_denied_before_epoch_advance(tmp_path: Path) -> None:
    effects, _reconciliations, _guard, confirmations, coordinator = _stack(tmp_path)
    effects.append_durable(_effect(state=EffectState.EFFECT_CONFIRMED))
    evidence = _evidence()
    authority = _authority(coordinator, evidence)

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            "fx-1",
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    assert exc_info.value.reason == "invalid_target_state"
    assert confirmations.current_epoch == 0
    assert authority.committed is False


def test_existing_terminal_reconciliation_denies_fresh_authority_before_epoch(
    tmp_path: Path,
) -> None:
    effects, reconciliations, _guard, confirmations, coordinator = _stack(tmp_path)
    raw = _effect()
    effects.append_durable(raw)
    evidence = _evidence()
    first_authority = _authority(coordinator, evidence)
    coordinator.resolve(
        raw.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence,
        first_authority,
    )
    assert confirmations.current_epoch == 1

    fresh = _authority(coordinator, evidence)
    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            fresh,
        )

    assert exc_info.value.reason in {"invalid_target_state", "already_reconciled"}
    assert confirmations.current_epoch == 1
    assert fresh.committed is False
    assert len(reconciliations.read_authoritative()) == 1


def test_clean_persistence_failure_commits_exact_fact_and_retry_survives_ttl(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _effect()
    effects.append_durable(raw)
    guard.hydrate()
    evidence = _evidence()
    clock = _Clock()
    authority = _authority(coordinator, evidence, clock=clock)
    real_append = reconciliations.append_durable
    calls = 0

    def fail_once(record: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ReconciliationLedgerError("injected clean persistence failure")
        real_append(record)  # type: ignore[arg-type]

    monkeypatch.setattr(reconciliations, "append_durable", fail_once)

    with pytest.raises(ReconciliationPersistenceError) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    first_record = exc_info.value.record
    assert exc_info.value.ambiguous is False
    assert authority.committed is True
    assert authority.consumed is False
    assert authority.committed_record is first_record
    assert confirmations.current_epoch == 1
    assert guard.require_clear(raw.semantic_key, refresh=False) is not None

    between_attempts = confirmations.issue(
        intent_hash="between",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark",
    )
    clock.value = 100.0

    resolution = coordinator.resolve(
        raw.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence,
        authority,
    )

    assert resolution.record is first_record
    assert resolution.record.reconciliation_id == "rec-1"
    assert resolution.record.timestamp == "2026-09-26T18:01:00+00:00"
    assert confirmations.current_epoch == 2
    assert authority.consumed is True
    assert len(reconciliations.read_authoritative()) == 1
    assert guard.require_clear(raw.semantic_key, refresh=False) is None

    _, denied_reason = confirmations.validate_and_consume(
        between_attempts.token,
        intent_hash="between",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark",
    )
    assert denied_reason == "stale_confirmation_epoch"


def test_foreign_direct_authority_cannot_bypass_operator_confirmation(
    tmp_path: Path,
) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _effect()
    effects.append_durable(raw)
    guard.hydrate()
    evidence = _evidence()
    foreign = ReconciliationAuthority(
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="operator-local",
        _protocol_key=object(),
        ttl_seconds=5.0,
        monotonic_clock=_Clock(),
        authority_id_factory=lambda: "foreign-auth",
    )

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            foreign,
        )

    assert exc_info.value.reason == "authority_protocol_mismatch"
    assert confirmations.current_epoch == 0
    assert foreign.committed is False
    assert reconciliations.read_authoritative() == []
    assert guard.require_clear(raw.semantic_key, refresh=False) is not None


def test_authority_binding_mismatch_cannot_advance_epoch(tmp_path: Path) -> None:
    effects, _reconciliations, _guard, confirmations, coordinator = _stack(tmp_path)
    raw = _effect()
    effects.append_durable(raw)
    evidence = _evidence()
    authority = _authority(
        coordinator,
        evidence,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
    )

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    assert exc_info.value.reason == "verdict_mismatch"
    assert confirmations.current_epoch == 0
    assert authority.committed is False
