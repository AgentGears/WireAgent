"""M6 Layer-4 canonical M5 live-attempt ownership regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    EffectVerb,
    ReconciliationAuthority,
    ReconciliationCoordinator,
    ReconciliationDenied,
    ReconciliationLedger,
    ReconciliationVerdict,
    RecoveryGuard,
    WriteIntent,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import (
    ApprovalGrant,
    ApprovalGrantStore,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "layer-4 live ownership"},
        actor_identity="@actor",
    )


def _gateway_stack(
    tmp_path: Path,
) -> tuple[
    WriteIntent,
    ApprovalGrant,
    EffectAttempt,
    CommitGateway,
    ReconciliationCoordinator,
]:
    intent = _intent()
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    epoch = AuthorizationEpoch()
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    store = ApprovalGrantStore()
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="123",
        policy_binding=policy.binding_hash(),
        authorization_epoch=epoch.current,
    )
    attempt = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        attempt.attempt_id,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        policy_binding=policy.binding_hash(),
        authorization_epoch=epoch.current,
    )
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=ConfirmationState(),
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: "rec-live",
        timestamp_factory=lambda: "2026-09-26T19:00:00+00:00",
    )
    return intent, grant, attempt, gateway, coordinator


def _evidence() -> dict[str, object]:
    return {
        "basis": "operator-review",
        "observed_at": "2026-09-26T18:59:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def _authority(
    coordinator: ReconciliationCoordinator,
    effect_id: str,
) -> ReconciliationAuthority:
    evidence = _evidence()
    return coordinator._mint_operator_authority(
        effect_id=effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="operator-local",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-live",
    )


def test_reserved_live_attempt_blocks_display_and_resolution_until_terminal_outcome(
    tmp_path: Path,
) -> None:
    intent, grant, attempt, gateway, coordinator = _gateway_stack(tmp_path)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is True

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.describe_target(attempt.effect_id)
    assert exc_info.value.reason == "live_attempt_owned"

    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            attempt.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            _evidence(),
            _authority(coordinator, attempt.effect_id),
        )
    assert exc_info.value.reason == "live_attempt_owned"

    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=permit.intent_hash,
        actor_id=permit.actor_id,
        target_type=permit.target_type,
        target_id=permit.target_id,
        policy_binding=permit.policy_binding,
    )
    gateway.record_effect_unknown(
        permit,
        attempt,
        evidence={"reason": "injected unknown external outcome"},
    )
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is False

    result = coordinator.resolve(
        attempt.effect_id,
        ReconciliationVerdict.CONFIRMED_EFFECT,
        _evidence(),
        _authority(coordinator, attempt.effect_id),
    )
    assert result.record.effect_id == attempt.effect_id
    assert result.record.verdict is ReconciliationVerdict.CONFIRMED_EFFECT


def test_reservation_io_failure_retains_live_ownership_before_permit_mint(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    intent, grant, attempt, gateway, coordinator = _gateway_stack(tmp_path)
    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected reservation fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)
    with pytest.raises(GatewayDenied, match="reservation_failed"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert calls >= 1
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is True

    # The lifecycle ownership check is deliberately before any ledger recovery
    # read. Even if reservation durability is locally ambiguous, reconciliation
    # cannot target an attempt that still owns the effect_id in this process.
    with pytest.raises(ReconciliationDenied) as exc_info:
        coordinator.resolve(
            attempt.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            _evidence(),
            _authority(coordinator, attempt.effect_id),
        )
    assert exc_info.value.reason == "live_attempt_owned"


def test_same_path_sibling_gateway_cannot_hide_runtime_live_owner(tmp_path: Path) -> None:
    intent, grant, attempt, runtime_gateway, _runtime_coordinator = _gateway_stack(tmp_path)
    runtime_gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    cfg = WebWireConfig(state_dir=tmp_path)
    sibling_effects = EffectLedger(path=cfg.effects_path())
    sibling_gateway = CommitGateway(
        ledger=sibling_effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    sibling_guard = RecoveryGuard(
        sibling_effects,
        reconciliation_ledger=reconciliations,
    )
    sibling_coordinator = ReconciliationCoordinator(
        recovery_guard=sibling_guard,
        confirmation_state=ConfirmationState(),
        commit_gateway=sibling_gateway,
        reconciliation_id_factory=lambda: "rec-sibling",
        timestamp_factory=lambda: "2026-09-26T19:02:00+00:00",
    )

    assert sibling_gateway.live_attempt_owns_effect(attempt.effect_id) is True
    with pytest.raises(ReconciliationDenied) as exc_info:
        sibling_coordinator.resolve(
            attempt.effect_id,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            _evidence(),
            _authority(sibling_coordinator, attempt.effect_id),
        )
    assert exc_info.value.reason == "live_attempt_owned"
