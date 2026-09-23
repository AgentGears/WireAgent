"""Kill/authority linearization regressions for the M5 Commit Gateway."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
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
        payload={"text": "kill race"},
        actor_identity="@actor",
    )


def _claim(intent: WriteIntent, epoch: AuthorizationEpoch):
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
    return grant, attempt


def test_programmatic_trip_cannot_activate_inside_permit_consume_boundary(
    tmp_path: Path,
) -> None:
    """Codex P1: trip and permit consumption have one linearization order.

    Pause consumption after it has observed an untripped kill state but before
    it marks the permit consumed. During that pause the KillSwitch state lock
    must still be owned by the consumer, so a concurrent ``trip()`` cannot set
    the flag / bump the epoch until consumption leaves the boundary.
    """
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    kill = KillSwitch(cfg)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    policy_binding = DEFAULT_EFFECT_POLICIES.require("post").binding_hash()

    inside_consume = threading.Event()
    release_consume = threading.Event()
    trip_started = threading.Event()
    trip_done = threading.Event()
    consume_done = threading.Event()
    original_binding = gateway._current_policy_binding

    def blocked_binding(action_type: str) -> str:
        inside_consume.set()
        assert release_consume.wait(timeout=5)
        return original_binding(action_type)

    gateway._current_policy_binding = blocked_binding  # type: ignore[method-assign]

    def consume() -> None:
        gateway.consume_permit(
            permit,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="123",
            policy_binding=policy_binding,
        )
        consume_done.set()

    def trip() -> None:
        trip_started.set()
        kill.trip()
        trip_done.set()

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert inside_consume.wait(timeout=5)

    # Consumption is paused after kill observation and must still hold the
    # shared kill-state lock. A different thread cannot acquire it now.
    assert kill._state_lock.acquire(blocking=False) is False

    tripper = threading.Thread(target=trip)
    tripper.start()
    assert trip_started.wait(timeout=5)
    assert trip_done.is_set() is False

    release_consume.set()
    consumer.join(timeout=10)
    tripper.join(timeout=10)
    assert not consumer.is_alive()
    assert not tripper.is_alive()

    assert consume_done.is_set() is True
    assert permit.consumed is True
    assert trip_done.is_set() is True
    assert kill.tripped() is True
    assert epoch.current == 1


def test_trip_before_boundary_denies_old_authority_even_after_reset(tmp_path: Path) -> None:
    """If trip linearizes first, reset cannot revive the old permit epoch."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    kill = KillSwitch(cfg)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    kill.trip()
    assert epoch.current == 1
    kill.reset()

    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        gateway.consume_permit(
            permit,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="123",
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.consumed is False
