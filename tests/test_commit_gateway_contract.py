"""Contract regressions found during the layer-3 independent pass."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
)
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float = 5000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _post_intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "contract"},
        actor_identity="@actor",
    )


def _claim(intent: WriteIntent, epoch: AuthorizationEpoch, clock: _Clock, binding: str):
    store = ApprovalGrantStore(clock=clock)
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="123",
        policy_binding=binding,
        authorization_epoch=epoch.current,
    )
    attempt = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        attempt.attempt_id,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        policy_binding=binding,
        authorization_epoch=epoch.current,
        now=clock(),
    )
    return grant, attempt


def _consume(gateway: CommitGateway, permit, intent: WriteIntent) -> None:  # type: ignore[no-untyped-def]
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type="post",
        target_id="123",
        policy_binding=permit.policy_binding,
    )


def test_kill_trip_invalidates_existing_permit_even_after_reset(tmp_path: Path) -> None:
    """Frozen invariant 11: activation revokes authority; reset is not revival."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    kill = KillSwitch(cfg)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
        clock=clock,
    )
    intent = _post_intent()
    binding = DEFAULT_EFFECT_POLICIES.require("post").binding_hash()
    grant, attempt = _claim(intent, epoch, clock, binding)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert permit.authorization_epoch == 0
    kill.trip()
    assert epoch.current == 1
    kill.reset()
    assert kill.tripped() is False

    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        _consume(gateway, permit, intent)
    assert permit.consumed is False


def test_policy_change_after_mint_invalidates_permit(tmp_path: Path) -> None:
    """Approval under policy P1 cannot execute after the registry becomes P2."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    kill = KillSwitch(cfg)
    policies = EffectPolicyRegistry()
    p1 = DEFAULT_EFFECT_POLICIES.require("post")
    policies.register(p1)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
        policies=policies,
        clock=clock,
    )
    intent = _post_intent()
    grant, attempt = _claim(intent, epoch, clock, p1.binding_hash())
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    p2 = EffectPolicy(
        action_type=p1.action_type,
        risk_tier=p1.risk_tier,
        allowed_effects=p1.allowed_effects,
        replay_semantics=p1.replay_semantics,
        durability=p1.durability,
        schema_version=p1.schema_version + 1,
    )
    policies.register(p2)
    assert p2.binding_hash() != permit.policy_binding

    with pytest.raises(GatewayDenied, match="policy_mismatch"):
        _consume(gateway, permit, intent)
    assert permit.consumed is False
