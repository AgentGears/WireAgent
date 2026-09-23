"""Codex exact-head review regressions for permit retention and terminal evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float = 9000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _intent(action: str, target_id: str = "123") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        payload={},
        actor_identity="@actor",
    )


def _claim(intent: WriteIntent, epoch: AuthorizationEpoch, clock: _Clock):
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
    store = ApprovalGrantStore(clock=clock)
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type=intent.action_type,
        target_type=intent.target_type,
        target_id=intent.target_id,
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
        now=clock(),
    )
    return grant, attempt


def _consume_bookmark(gateway: CommitGateway, permit, intent: WriteIntent) -> None:  # type: ignore[no-untyped-def]
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SET_BOOKMARK,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=DEFAULT_EFFECT_POLICIES.require("bookmark").binding_hash(),
    )


def test_non_fenced_confirmation_is_durable_and_terminal_permit_is_evicted(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=10.0,
    )
    intent = _intent("bookmark")
    grant, attempt = _claim(intent, epoch, clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume_bookmark(gateway, permit, intent)

    gateway.record_effect_confirmed(permit, attempt, evidence={"state": "bookmarked"})

    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].state is EffectState.EFFECT_CONFIRMED
    assert records[0].semantic_key == intent.dedupe_key()
    assert permit.permit_id not in gateway._issued_permits
    with pytest.raises(GatewayDenied, match="permit_reused"):
        _consume_bookmark(gateway, permit, intent)


def test_authorize_prunes_expired_unused_permits(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=10.0,
    )

    first = _intent("bookmark", "first")
    grant1, attempt1 = _claim(first, epoch, clock)
    permit1 = gateway.authorize_commit(grant=grant1, attempt=attempt1, intent=first)
    assert permit1.permit_id in gateway._issued_permits

    clock.now = permit1.expires_at + 1
    second = _intent("bookmark", "second")
    grant2, attempt2 = _claim(second, epoch, clock)
    permit2 = gateway.authorize_commit(grant=grant2, attempt=attempt2, intent=second)

    assert permit1.permit_id not in gateway._issued_permits
    assert permit2.permit_id in gateway._issued_permits
