"""Outcome-persistence ordering regression for M5 layer 3."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayStateError
from webwire.safety.effect_ledger import EffectLedger, EffectLedgerError, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import ApprovalGrantStore, AttemptState, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _FailSecondAppendLedger(EffectLedger):
    """Reservation succeeds; first terminal append fails; retry succeeds."""

    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self.calls = 0

    def append_durable(self, record):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 2:
            raise EffectLedgerError("injected terminal fsync failure")
        super().append_durable(record)


def test_unknown_outcome_persistence_can_retry_without_restoring_execution(
    tmp_path: Path,
) -> None:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    intent = WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "unknown persistence"},
        actor_identity="@actor",
    )
    cfg = WebWireConfig(state_dir=tmp_path)
    ledger = _FailSecondAppendLedger(cfg)
    epoch = AuthorizationEpoch()
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
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type="post",
        target_id="123",
        policy_binding=policy.binding_hash(),
    )

    with pytest.raises(GatewayStateError, match="persist unknown outcome"):
        gateway.record_effect_unknown(permit, attempt, evidence={"reason": "timeout"})

    # The persistence failure must not reopen execution authority, but it must
    # leave the attempt in the pre-terminal state so outcome persistence itself
    # can be retried.
    assert permit.consumed is True
    assert attempt.state is AttemptState.RESERVED

    gateway.record_effect_unknown(permit, attempt, evidence={"reason": "timeout"})
    assert attempt.state is AttemptState.EFFECT_UNKNOWN
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN
