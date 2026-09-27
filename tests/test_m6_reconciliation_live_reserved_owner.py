"""A still-authorized RESERVED attempt remains a reconciliation live owner."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import EffectLedger, EffectState, WriteIntent
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import (
    ApprovalGrant,
    ApprovalGrantStore,
    AuthorizationEpoch,
    EffectAttempt,
    GrantState,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def test_reserved_attempt_with_live_grant_is_not_auto_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    effects = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    risk, compensation = DEFAULT_REGISTRY.require("post")
    intent = WriteIntent(
        action_type="post",
        target_type="post",
        target_id="live-reserved",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "still authorized"},
        actor_identity="@actor",
    )
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    grant = ApprovalGrantStore(ttl_seconds=300.0).mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="live-reserved",
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

    real_spend = ApprovalGrant.spend_if_live

    def fail_after_reservation(self: ApprovalGrant, **kwargs: object) -> None:
        if self is grant:
            raise RuntimeError("injected failure after durable reservation")
        real_spend(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(ApprovalGrant, "spend_if_live", fail_after_reservation)

    with pytest.raises(RuntimeError, match="after durable reservation"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert attempt.state.value == "reserved"
    assert grant.state is GrantState.ACTIVE
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is True
    assert gateway.settle_dead_prepermit_owner_for_reconciliation(attempt.effect_id) is True
    assert attempt.state.value == "reserved"
    assert grant.state is GrantState.ACTIVE
    assert [record.state for record in effects.read_records()] == [EffectState.RESERVED]
