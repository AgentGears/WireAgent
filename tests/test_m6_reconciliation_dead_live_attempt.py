"""M6 Layer-4 retirement of authority-dead pre-permit live owners."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import EffectLedger, EffectState, WriteIntent
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedgerError
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


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="dead-owner",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "dead prepermit owner"},
        actor_identity="@actor",
    )


def _stack(
    tmp_path: Path,
    *,
    grant_clock: _Clock | None = None,
    grant_ttl_seconds: float = 60.0,
) -> tuple[
    WebWireConfig,
    WriteIntent,
    ApprovalGrant,
    EffectAttempt,
    EffectLedger,
    AuthorizationEpoch,
    CommitGateway,
]:
    cfg = WebWireConfig(state_dir=tmp_path)
    intent = _intent()
    effects = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    clock = grant_clock or _Clock()
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    store = ApprovalGrantStore(clock=clock, ttl_seconds=grant_ttl_seconds)
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="dead-owner",
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
    return cfg, intent, grant, attempt, effects, epoch, gateway


def _fail_first_reservation_fsync(
    monkeypatch: pytest.MonkeyPatch,
    gateway: CommitGateway,
    grant: ApprovalGrant,
    attempt: EffectAttempt,
    intent: WriteIntent,
) -> None:
    real_fsync = os.fsync
    calls = 0

    def fail_first(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected reservation fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first)
    with pytest.raises(GatewayDenied) as exc_info:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    monkeypatch.setattr(os, "fsync", real_fsync)

    assert exc_info.value.reason == "reservation_failed"
    assert calls >= 1
    assert attempt.reservation_started is True


def test_expired_grant_after_reservation_failure_closes_no_effect_and_retires(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    _cfg, intent, grant, attempt, effects, _epoch, gateway = _stack(
        tmp_path,
        grant_clock=clock,
        grant_ttl_seconds=1.0,
    )
    _fail_first_reservation_fsync(monkeypatch, gateway, grant, attempt, intent)

    assert gateway.live_attempt_owns_effect(attempt.effect_id) is True
    clock.value = 11.0

    assert gateway.live_attempt_owns_effect(attempt.effect_id) is False
    assert grant.state is GrantState.EXPIRED
    assert attempt.state.value == "no_effect"
    records = effects.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    assert records[-1].details["reason"] == "expired_before_permit"


def test_sibling_gateway_uses_originating_epoch_for_dead_owner_retirement(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    cfg, intent, grant, attempt, effects, epoch, gateway = _stack(tmp_path)
    _fail_first_reservation_fsync(monkeypatch, gateway, grant, attempt, intent)

    sibling = CommitGateway(
        ledger=EffectLedger(path=effects.path),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(initial=99),
    )

    # The sibling's unrelated epoch must not revoke the runtime grant.
    assert sibling.live_attempt_owns_effect(attempt.effect_id) is True
    assert grant.state is GrantState.ACTIVE

    # Revocation in the originating epoch domain permanently kills retry
    # authority; the sibling may then safely drive the exact durable closure.
    epoch.bump()
    assert sibling.live_attempt_owns_effect(attempt.effect_id) is False
    assert grant.state is GrantState.REVOKED
    assert [record.state for record in effects.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]


def test_minted_unconsumed_permit_is_never_auto_retired_by_live_owner_check(
    tmp_path: Path,
) -> None:
    cfg, intent, grant, attempt, effects, _epoch, gateway = _stack(tmp_path)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert permit.consumed is False
    assert grant.state is GrantState.SPENT

    sibling = CommitGateway(
        ledger=EffectLedger(path=effects.path),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(initial=99),
    )

    assert sibling.live_attempt_owns_effect(attempt.effect_id) is True
    assert permit.consumed is False
    assert attempt.state.value == "reserved"
    assert [record.state for record in effects.read_records()] == [EffectState.RESERVED]


def test_failed_no_effect_report_retries_exact_closure_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    clock = _Clock()
    _cfg, intent, grant, attempt, effects, _epoch, gateway = _stack(
        tmp_path,
        grant_clock=clock,
        grant_ttl_seconds=1.0,
    )
    _fail_first_reservation_fsync(monkeypatch, gateway, grant, attempt, intent)
    clock.value = 11.0

    real_append = effects.append_durable
    failed_after_durable = False

    def fail_once_after_no_effect_is_durable(record: object) -> None:
        nonlocal failed_after_durable
        real_append(record)  # type: ignore[arg-type]
        if (
            not failed_after_durable
            and getattr(record, "state", None) is EffectState.NO_EFFECT
        ):
            failed_after_durable = True
            raise EffectLedgerError("injected post-durable NO_EFFECT report failure")

    monkeypatch.setattr(effects, "append_durable", fail_once_after_no_effect_is_durable)

    # The fact may already be durable, but the gateway did not receive success;
    # ownership therefore remains conservative until exact re-durability wins.
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is True
    assert failed_after_durable is True
    first_history = effects.read_records()
    assert [record.state for record in first_history] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    closure_reason = first_history[-1].details["reason"]
    assert closure_reason == "expired_before_permit"

    monkeypatch.setattr(effects, "append_durable", real_append)
    assert gateway.live_attempt_owns_effect(attempt.effect_id) is False

    final_history = effects.read_records()
    assert len(final_history) == 2
    assert final_history[-1].state is EffectState.NO_EFFECT
    assert final_history[-1].details["reason"] == closure_reason
