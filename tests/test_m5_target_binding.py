"""M5 target-lineage validity regressions.

BEST_EFFORT skips the precommit EffectLedger reservation, so CommitGateway must
reject target bindings that could not be represented by a later terminal ledger
fact before it mints execution authority.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
)
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantState,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def _claimed_bookmark(
    *,
    target_type: str,
    target_id: str,
    epoch: AuthorizationEpoch,
) -> tuple[WriteIntent, object, EffectAttempt]:
    risk, compensation = DEFAULT_REGISTRY.require("bookmark")
    policy = DEFAULT_EFFECT_POLICIES.require("bookmark")
    assert policy.durability is DurabilityPolicy.BEST_EFFORT

    intent = WriteIntent(
        action_type="bookmark",
        target_type=target_type,
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        payload={"post_url": "https://x.com/a/status/123"},
        actor_identity="@actor",
    )
    store = ApprovalGrantStore()
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="bookmark",
        target_type=target_type,
        target_id=target_id,
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
    return intent, grant, attempt


@pytest.mark.parametrize(
    ("target_type", "target_id"),
    [
        ("", "123"),
        ("post", ""),
    ],
    ids=["empty-target-type", "empty-target-id"],
)
def test_best_effort_empty_target_is_denied_before_authority(
    tmp_path: Path,
    target_type: str,
    target_id: str,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent, grant, attempt = _claimed_bookmark(
        target_type=target_type,
        target_id=target_id,
        epoch=epoch,
    )

    with pytest.raises(GatewayDenied) as denied:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert denied.value.reason == "target_missing"
    assert grant.state is GrantState.ACTIVE
    assert grant.claimed_by == attempt.attempt_id
    assert attempt.state is AttemptState.PREPARING
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}
    assert ledger.read_records() == []
