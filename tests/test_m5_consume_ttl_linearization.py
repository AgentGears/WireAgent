"""Maintainer regression for permit TTL at the actual consume boundary."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
)
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _BlockingPolicies(EffectPolicyRegistry):
    """Pause one policy fence after its registry lock is acquired."""

    def __init__(self, policy: EffectPolicy) -> None:
        super().__init__()
        self.register(policy)
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def policy_fence(self, action_type: str) -> Iterator[EffectPolicy]:
        with super().policy_fence(action_type) as policy:
            if self.block:
                self.entered.set()
                assert self.release.wait(timeout=5)
            yield policy


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("bookmark")
    return WriteIntent(
        action_type="bookmark",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        actor_identity="@actor",
        payload={"post_url": "https://x.com/a/status/123", "post_id": "123"},
    )


def test_permit_expiring_while_policy_fence_blocks_is_not_consumed(
    tmp_path: Path,
) -> None:
    """TTL is re-read after blocking fences at the mutation-authority edge."""
    clock = _Clock()
    epoch = AuthorizationEpoch()
    policy = DEFAULT_EFFECT_POLICIES.require("bookmark")
    policies = _BlockingPolicies(policy)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        policies=policies,
        clock=clock,
        permit_ttl_seconds=5.0,
    )
    intent = _intent()
    grant = ApprovalGrantStore(clock=clock, ttl_seconds=300.0).mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="bookmark",
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
        now=clock(),
    )
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert permit.expires_at == 105.0

    policies.block = True
    result: list[object] = []

    def consume() -> None:
        try:
            gateway.consume_permit(
                permit,
                effect=EffectVerb.SET_BOOKMARK,
                intent_hash=intent.intent_hash(),
                actor_id="@actor",
                target_type="post",
                target_id="123",
                policy_binding=policy.binding_hash(),
            )
            result.append("consumed")
        except Exception as exc:  # pragma: no cover - asserted below
            result.append(exc)

    thread = threading.Thread(target=consume)
    thread.start()
    assert policies.entered.wait(timeout=5)

    # The permit was live when consume started, but expires while the caller is
    # blocked waiting inside a policy fence. A stale pre-wait clock sample must
    # never authorize the later mutation boundary.
    clock.now = 106.0
    policies.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], GatewayDenied)
    assert result[0].reason == "permit_expired"
    assert permit.consumed is False
    assert permit.consumed_at is None
    assert attempt.state is AttemptState.NO_EFFECT
    assert permit.permit_id not in gateway._issued_permits
    assert permit.permit_id not in gateway._issued_attempts
