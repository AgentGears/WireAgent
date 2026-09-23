"""Outcome-lineage regressions for the M5 Commit Gateway."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayStateError
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AuthorizationEpoch,
    EffectAttempt,
)
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
        payload={"text": "lineage"},
        actor_identity="@actor",
    )


def test_outcome_rejects_attempt_with_matching_id_but_wrong_grant(tmp_path: Path) -> None:
    """Codex P2: outcome recording validates attempt and grant lineage together."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    policy = DEFAULT_EFFECT_POLICIES.require("post")

    store = ApprovalGrantStore()
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
    )
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=policy.binding_hash(),
    )

    forged = EffectAttempt(
        grant_id="different-grant",
        attempt_id=attempt.attempt_id,
        state=attempt.state,
    )
    with pytest.raises(GatewayStateError, match="grant mismatch"):
        gateway.record_effect_confirmed(permit, forged)

    # The rejected object cannot consume the terminal transition. The real
    # attempt and permit remain available only for outcome persistence.
    assert forged.state is attempt.state
    assert attempt.state.value == "reserved"
    assert permit.permit_id in gateway._issued_permits
    assert [record.state for record in ledger.read_records()] == [EffectState.RESERVED]

    gateway.record_effect_confirmed(permit, attempt, evidence={"source": "real-attempt"})
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    assert records[-1].details["grant_id"] == grant.grant_id
