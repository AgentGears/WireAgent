"""M6 Layer-5 qualification of same-path coordinator concurrency domains."""

from __future__ import annotations

import threading
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


def _evidence(label: str) -> dict[str, object]:
    return {
        "basis": "layer5-sibling-coordinator",
        "observed_at": "2026-09-27T18:10:00+00:00",
        "observations": [{"kind": "operator", "value": label}],
    }


def _coordinator(
    cfg: WebWireConfig,
    *,
    effects: EffectLedger,
    reconciliations: ReconciliationLedger,
    confirmations: ConfirmationState,
) -> ReconciliationCoordinator:
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    return ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
    )


def _authority(
    coordinator: ReconciliationCoordinator,
    *,
    effect_id: str,
    verdict: ReconciliationVerdict,
    evidence: dict[str, object],
    authority_id: str,
):
    return coordinator._mint_operator_authority(
        effect_id=effect_id,
        verdict=verdict,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: authority_id,
    )


def _raw() -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id="fx-r29-sibling",
        semantic_key="actor|like|post|r29-sibling|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-r29-sibling",
        policy_binding="policy-r29-sibling",
        actor_id="actor",
        target_type="post",
        target_id="r29-sibling",
        timestamp="2026-09-27T18:09:00+00:00",
    )


def test_r29_same_path_sibling_coordinators_share_one_terminal_order(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    confirmations = ConfirmationState()

    effects_a = EffectLedger(cfg)
    reconciliations_a = ReconciliationLedger(path=cfg.reconciliations_path())
    coordinator_a = _coordinator(
        cfg,
        effects=effects_a,
        reconciliations=reconciliations_a,
        confirmations=confirmations,
    )

    effects_b = EffectLedger(cfg)
    reconciliations_b = ReconciliationLedger(path=cfg.reconciliations_path())
    coordinator_b = _coordinator(
        cfg,
        effects=effects_b,
        reconciliations=reconciliations_b,
        confirmations=confirmations,
    )

    raw = _raw()
    effects_a.append_durable(raw)
    coordinator_a.recovery_guard.hydrate()
    coordinator_b.recovery_guard.hydrate()

    evidence_a = _evidence("effect")
    evidence_b = _evidence("no-effect")
    authority_a = _authority(
        coordinator_a,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence_a,
        authority_id="auth-r29-sibling-a",
    )
    authority_b = _authority(
        coordinator_b,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        evidence=evidence_b,
        authority_id="auth-r29-sibling-b",
    )

    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def resolve(
        coordinator: ReconciliationCoordinator,
        verdict: ReconciliationVerdict,
        evidence: dict[str, object],
        authority: object,
    ) -> None:
        barrier.wait()
        try:
            coordinator.resolve(
                raw.effect_id,
                verdict,
                evidence,
                authority,  # type: ignore[arg-type]
            )
            outcome = "committed"
        except ReconciliationDenied as exc:
            outcome = exc.reason
        with outcome_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(
            target=resolve,
            args=(
                coordinator_a,
                ReconciliationVerdict.CONFIRMED_EFFECT,
                evidence_a,
                authority_a,
            ),
        ),
        threading.Thread(
            target=resolve,
            args=(
                coordinator_b,
                ReconciliationVerdict.CONFIRMED_NO_EFFECT,
                evidence_b,
                authority_b,
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(outcomes) == ["already_reconciled", "committed"]
    assert confirmations.current_epoch == 1
    records = ReconciliationLedger(path=cfg.reconciliations_path()).read_authoritative()
    assert len(records) == 1
    assert records[0].effect_id == raw.effect_id


def test_same_path_coordinator_rejects_a_second_confirmation_epoch_domain(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects_a = EffectLedger(cfg)
    reconciliations_a = ReconciliationLedger(path=cfg.reconciliations_path())
    _coordinator(
        cfg,
        effects=effects_a,
        reconciliations=reconciliations_a,
        confirmations=ConfirmationState(),
    )

    effects_b = EffectLedger(cfg)
    reconciliations_b = ReconciliationLedger(path=cfg.reconciliations_path())
    with pytest.raises(ValueError, match="must share one ConfirmationState"):
        _coordinator(
            cfg,
            effects=effects_b,
            reconciliations=reconciliations_b,
            confirmations=ConfirmationState(),
        )
