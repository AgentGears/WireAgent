"""Fault-injection regressions for stable attempt/effect identity at the gateway."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import (
    CommitGateway,
    EffectPermit,
    GatewayDenied,
    GatewayStateError,
)
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import (
    ApprovalGrant,
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantState,
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
        payload={"text": "stable-effect-id"},
        actor_identity="@actor",
    )


def _claimed(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
) -> tuple[ApprovalGrant, EffectAttempt]:
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
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
    return grant, attempt


def _consume(
    gateway: CommitGateway,
    permit: EffectPermit,
    intent: WriteIntent,
) -> None:
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
    )


def test_written_then_fsync_failed_reservation_retries_same_effect_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    ledger = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claimed(intent, epoch)

    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected reservation fsync failure after write")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)

    with pytest.raises(GatewayDenied, match="reservation_failed"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    # The bytes can already be visible even though durability was reported as
    # failed. No permit was minted, and the approval remains claimed/ACTIVE.
    first = ledger.read_records()
    assert len(first) == 1
    assert first[0].state is EffectState.RESERVED
    assert first[0].effect_id == attempt.effect_id
    assert attempt.state is AttemptState.PREPARING
    assert grant.state is GrantState.ACTIVE
    assert grant.claimed_by == attempt.attempt_id

    # Same attempt retries the SAME effect fact. EffectLedger re-fsyncs that
    # fact instead of appending a second reservation with a fresh effect id.
    permit = gateway.authorize_commit(
        grant=grant,
        attempt=attempt,
        intent=intent,
    )
    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].effect_id == attempt.effect_id == permit.effect_id
    assert attempt.state is AttemptState.RESERVED
    assert grant.state is GrantState.SPENT

    _consume(gateway, permit, intent)
    gateway.record_effect_confirmed(
        permit,
        attempt,
        evidence={"proof": "same-lineage"},
    )
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    assert {record.effect_id for record in records} == {attempt.effect_id}


def test_invalid_terminal_evidence_fails_without_losing_retryable_outcome(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    ledger = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claimed(intent, epoch)
    permit = gateway.authorize_commit(
        grant=grant,
        attempt=attempt,
        intent=intent,
    )
    _consume(gateway, permit, intent)

    with pytest.raises(GatewayStateError, match="invalid effect evidence"):
        gateway.record_effect_confirmed(
            permit,
            attempt,
            evidence={"not_json": object()},
        )

    assert permit.consumed is True
    assert attempt.state is AttemptState.RESERVED
    assert permit.permit_id in gateway._issued_permits
    assert [record.state for record in ledger.read_records()] == [EffectState.RESERVED]

    gateway.record_effect_confirmed(
        permit,
        attempt,
        evidence={"proof": "valid"},
    )
    assert attempt.state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].state is EffectState.EFFECT_CONFIRMED


def test_mutated_attempt_effect_identity_is_rejected_at_outcome_boundary(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claimed(intent, epoch)
    permit = gateway.authorize_commit(
        grant=grant,
        attempt=attempt,
        intent=intent,
    )
    _consume(gateway, permit, intent)

    attempt.effect_id = "mutated"
    with pytest.raises(GatewayStateError, match="effect mismatch"):
        gateway.record_effect_confirmed(permit, attempt)

    assert permit.consumed is True
    assert attempt.state is AttemptState.RESERVED
