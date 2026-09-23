"""Review reconciliation regressions for M5 layer 3."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "review"},
        actor_identity="@actor",
    )


def _claim(intent: WriteIntent, epoch: AuthorizationEpoch):
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
    return grant, attempt


def test_codex_p1_listener_added_during_preobserved_trip_gets_revocation(
    tmp_path: Path,
) -> None:
    """A gateway created during an already-observed trip must bump the epoch."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    intent = _intent()
    grant, attempt = _claim(intent, epoch)

    kill = KillSwitch(cfg)
    kill.trip()
    assert kill.tripped() is True  # trip was observed before gateway/listener exists
    assert epoch.current == 0

    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )
    assert epoch.current == 1

    kill.reset()
    assert kill.tripped() is False
    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)


def test_permit_binding_is_immutable_and_copies_are_not_authority(tmp_path: Path) -> None:
    """Only the exact immutable permit minted by this gateway is consumable."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(FrozenInstanceError):
        permit.target_id = "B"  # type: ignore[misc]

    forged = replace(permit, target_id="B")
    with pytest.raises(GatewayDenied, match="permit_unknown"):
        gateway.consume_permit(
            forged,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="B",
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.consumed is False
