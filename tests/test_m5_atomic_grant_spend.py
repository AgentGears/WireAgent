"""Regressions for atomic grant liveness validation at the spend boundary."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


class _ProgrammableGrantClock(_Clock):
    """Grant clock that can expire itself or advance the permit clock."""

    def __init__(self, now: float) -> None:
        super().__init__(now)
        self.expire_on_call: Optional[int] = None
        self.expire_value: Optional[float] = None
        self.advance_on_call: Optional[int] = None
        self.other_clock: Optional[_Clock] = None
        self.other_delta = 0.0

    def __call__(self) -> float:
        self.calls += 1
        if self.expire_on_call == self.calls:
            assert self.expire_value is not None
            self.now = self.expire_value
        if self.advance_on_call == self.calls:
            assert self.other_clock is not None
            self.other_clock.now += self.other_delta
        return self.now


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


def test_gateway_rejects_grant_expiring_at_atomic_spend(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    grant_clock = _ProgrammableGrantClock(1_000.0)
    epoch = AuthorizationEpoch()
    intent = _post_intent()
    grant, attempt, _binding = _claimed_grant(intent, epoch, grant_clock)
    # authorize_commit first validates once, then spend_if_live must refresh the
    # grant again at the exact transition. Expire on that second authorization read.
    grant_clock.expire_on_call = grant_clock.calls + 2
    grant_clock.expire_value = grant.expires_at
    gateway_clock = _Clock(20_000.0)
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
    # Only housekeeping sampled the permit clock; no permit timestamp is taken
    # after the atomic spend rejects the expired approval.
    assert gateway_clock.calls == 1
    assert grant.state is GrantState.EXPIRED
    assert attempt.state is AttemptState.NO_EFFECT
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}


def test_slow_grant_spend_does_not_consume_new_permit_ttl(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    grant_clock = _ProgrammableGrantClock(1_000.0)
    gateway_clock = _Clock(20_000.0)
    epoch = AuthorizationEpoch()
    intent = _post_intent()
    grant, attempt, _binding = _claimed_grant(intent, epoch, grant_clock)

    # Simulate elapsed permit-clock time while the final grant-clock read runs.
    # If mint_now were sampled before spend_if_live, the returned permit would
    # already be expired by 70 seconds. Correct ordering samples permit time after.
    grant_clock.advance_on_call = grant_clock.calls + 2
    grant_clock.other_clock = gateway_clock
    grant_clock.other_delta = 100.0

    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=gateway_clock,
        permit_ttl_seconds=30.0,
    )

    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert grant.state is GrantState.SPENT
    assert permit.issued_at == 20_100.0
    assert permit.expires_at == 20_130.0
    assert gateway_clock.now < permit.expires_at
    assert gateway_clock.calls == 2
