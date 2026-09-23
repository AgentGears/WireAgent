"""Layer-3 fault injection against the real EffectLedger implementation."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt, GrantState
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def test_t1_real_ledger_fsync_failure_mints_no_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """T1 full primitive integration: a reservation fsync failure is fail closed."""
    risk, compensation = DEFAULT_REGISTRY.require("post")
    intent = WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "fsync fault"},
        actor_identity="@actor",
    )
    cfg = WebWireConfig(state_dir=tmp_path)
    ledger = EffectLedger(cfg)
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

    real_fsync = os.fsync
    calls = 0

    def fail_first_fsync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected reservation fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_fsync)

    with pytest.raises(GatewayDenied, match="reservation_failed"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert calls >= 1
    assert grant.state is GrantState.ACTIVE
    assert grant.claimed_by == attempt.attempt_id
