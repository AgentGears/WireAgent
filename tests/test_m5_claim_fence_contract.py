"""Gateway-facing claim-fence contract regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def test_authorize_without_claim_is_gateway_denial_not_model_error(tmp_path: Path) -> None:
    """The model lock must not leak through the CommitGateway API boundary."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    risk, compensation = DEFAULT_REGISTRY.require("post")
    intent = WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "claim-fence"},
        actor_identity="@actor",
    )
    grant = ApprovalGrantStore().mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="123",
        policy_binding=policy.binding_hash(),
        authorization_epoch=epoch.current,
    )
    attempt = EffectAttempt(grant_id=grant.grant_id)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )

    with pytest.raises(GatewayDenied) as exc_info:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert exc_info.value.reason == "claim_not_held"
    assert grant.claimed_by is None
