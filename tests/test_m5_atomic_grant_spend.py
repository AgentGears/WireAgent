"""Regressions for atomic grant liveness validation at the spend boundary."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantClaimDenied,
    GrantState,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _ExpireGrantOnSecondGatewayRead:
    """Expire the grant during the final gateway mint-clock read."""

    def __init__(self, grant_clock: _Clock, expires_at: float) -> None:
        self._grant_clock = grant_clock
        self._expires_at = expires_at
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        if self.calls == 2:
            self._grant_clock.now = self._expires_at
        return 20_000.0 + self.calls


def _post_intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "atomic spend"},
        actor_identity="@actor",
    )


def _claimed_grant(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
    grant_clock: _Clock,
):
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    store = ApprovalGrantStore(clock=grant_clock, ttl_seconds=5.0)
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
    return grant, attempt, policy.binding_hash()


def test_spend_if_live_refreshes_expiry_at_transition() -> None:
    grant_clock = _Clock(1_000.0)
    epoch = AuthorizationEpoch()
    intent = _post_intent()
    grant, _attempt, binding = _claimed_grant(intent, epoch, grant_clock)

    grant_clock.now = grant.expires_at
    with pytest.raises(GrantClaimDenied) as denied:
        grant.spend_if_live(
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            policy_binding=binding,
            authorization_epoch=epoch.current,
        )

    assert denied.value.reason == "expired"
    assert grant.state is GrantState.EXPIRED


def test_gateway_rejects_grant_expiring_during_final_mint_clock(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    grant_clock = _Clock(1_000.0)
    epoch = AuthorizationEpoch()
    intent = _post_intent()
    grant, attempt, _binding = _claimed_grant(intent, epoch, grant_clock)
    gateway_clock = _ExpireGrantOnSecondGatewayRead(grant_clock, grant.expires_at)
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=gateway_clock,
    )

    with pytest.raises(GatewayDenied) as denied:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert denied.value.reason == "expired"
    assert gateway_clock.calls == 2
    assert grant.state is GrantState.EXPIRED
    assert attempt.state is AttemptState.NO_EFFECT
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}
