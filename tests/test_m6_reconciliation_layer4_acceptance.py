"""Focused frozen M6 acceptance cases owned by Layer 4."""

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
    ReconciliationPublicationError,
    ReconciliationVerdict,
    RecoveryGuard,
    RecoveryGuardUnavailable,
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
        "observed_at": "2026-09-26T20:30:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def _raw(state: EffectState, *, effect_id: str = "fx-accept") -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=f"actor|like|post|{effect_id}|",
        state=state,
        action_type="like",
        intent_hash=f"intent-{effect_id}",
        policy_binding="policy-accept",
        actor_id="actor",
        target_type="post",
        target_id=effect_id,
        timestamp="2026-09-26T20:29:00+00:00",
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
        reconciliation_id_factory=lambda: "rec-accept",
        timestamp_factory=lambda: "2026-09-26T20:31:00+00:00",
    )
    return effects, reconciliations, guard, confirmations, coordinator


def _authority(
    coordinator: ReconciliationCoordinator,
    *,
    effect_id: str,
    verdict: ReconciliationVerdict,
    evidence: dict[str, object],
    clock: _Clock | None = None,
    ttl_seconds: float = 120.0,
):
    return coordinator._mint_operator_authority(
        effect_id=effect_id,
        verdict=verdict,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=ttl_seconds,
        monotonic_clock=clock or _Clock(),
        authority_id_factory=lambda: "auth-accept",
    )


@pytest.mark.parametrize("raw_state", [EffectState.RESERVED, EffectState.EFFECT_UNKNOWN])
@pytest.mark.parametrize(
    "verdict",
    [
        ReconciliationVerdict.CONFIRMED_EFFECT,
        ReconciliationVerdict.CONFIRMED_NO_EFFECT,
    ],
)
def test_both_terminal_verdicts_resolve_both_reconciliable_raw_states(
    tmp_path: Path,
    raw_state: EffectState,
    verdict: ReconciliationVerdict,
) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _raw(raw_state)
    effects.append_durable(raw)
    guard.hydrate()
    evidence = _evidence()
    authority = _authority(
        coordinator,
        effect_id=raw.effect_id,
        verdict=verdict,
        evidence=evidence,
    )

    result = coordinator.resolve(raw.effect_id, verdict, evidence, authority)

    assert result.record.verdict is verdict
    assert result.record.effect_id == raw.effect_id
    assert confirmations.current_epoch == 1
    assert authority.consumed is True
    assert guard.require_clear(raw.semantic_key, refresh=False) is None
    assert reconciliations.read_authoritative() == [result.record]
    assert effects.read_records() == [raw]


def test_expired_authority_denies_before_epoch_or_persistence(tmp_path: Path) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _raw(EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    guard.hydrate()
    evidence = _evidence()
    clock = _Clock()
    authority = _authority(
        coordinator,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence,
        clock=clock,
        ttl_seconds=1.0,
    )
    clock.value = 11.0

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    assert exc_info.value.reason == "authority_expired"
    assert confirmations.current_epoch == 0
    assert authority.committed is False
    assert reconciliations.read_authoritative() == []
    assert guard.require_clear(raw.semantic_key, refresh=False) is not None


def test_unknown_effect_denies_before_epoch_or_persistence(tmp_path: Path) -> None:
    _effects, reconciliations, _guard, confirmations, coordinator = _stack(tmp_path)
    evidence = _evidence()
    authority = _authority(
        coordinator,
        effect_id="fx-missing",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence,
    )

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            "fx-missing",
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    assert exc_info.value.reason == "unknown_effect_id"
    assert confirmations.current_epoch == 0
    assert authority.committed is False
    assert reconciliations.read_authoritative() == []


def test_durable_fact_with_guard_publication_failure_stays_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    effects, reconciliations, guard, confirmations, coordinator = _stack(tmp_path)
    raw = _raw(EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    guard.hydrate()
    evidence = _evidence()
    authority = _authority(
        coordinator,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence,
    )

    def fail_refresh():
        raise RecoveryGuardUnavailable("injected publication failure")

    monkeypatch.setattr(guard, "refresh", fail_refresh)

    with pytest.raises(ReconciliationPublicationError) as exc_info:
        coordinator.resolve(
            raw.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence,
            authority,
        )

    assert exc_info.value.record.effect_id == raw.effect_id
    assert confirmations.current_epoch == 1
    assert authority.consumed is True
    records = reconciliations.read_authoritative()
    assert len(records) == 1
    assert records[0].effect_id == raw.effect_id
    # The previous blocked cache was never replaced by clear truth.
    assert guard.require_clear(raw.semantic_key, refresh=False) is not None
