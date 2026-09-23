"""M5 layer-3 tests for the Commit Gateway transaction protocol."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger, EffectLedgerError, EffectState
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


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FailingLedger(EffectLedger):
    def append_durable(self, record):  # type: ignore[no-untyped-def]
        raise EffectLedgerError("injected reservation failure")


def _intent(action: str = "post", *, target_id: str = "123") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    payload = {"text": "hello"} if action in {"post", "reply", "quote"} else {}
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        payload=payload,
        actor_identity="@actor",
    )


def _claimed(
    intent: WriteIntent,
    *,
    epoch: AuthorizationEpoch,
    clock: _Clock,
) -> tuple[ApprovalGrant, EffectAttempt]:
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
    store = ApprovalGrantStore(clock=clock)
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id=intent.actor_identity or "",
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
        actor_id=intent.actor_identity or "",
        policy_binding=policy.binding_hash(),
        authorization_epoch=epoch.current,
        now=clock(),
    )
    return grant, attempt


def _gateway(
    tmp_path: Path,
    *,
    clock: _Clock | None = None,
    ledger: EffectLedger | None = None,
) -> tuple[CommitGateway, EffectLedger, KillSwitch, AuthorizationEpoch, _Clock]:
    c = clock or _Clock()
    cfg = WebWireConfig(state_dir=tmp_path)
    real_ledger = ledger or EffectLedger(cfg)
    kill = KillSwitch(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=real_ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
        clock=c,
        permit_ttl_seconds=10.0,
    )
    return gateway, real_ledger, kill, epoch, c


def _consume_post(gateway: CommitGateway, permit, intent: WriteIntent) -> None:  # type: ignore[no-untyped-def]
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id=intent.actor_identity or "",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=DEFAULT_EFFECT_POLICIES.require(intent.action_type).binding_hash(),
    )


def test_t1_reservation_failure_mints_no_permit_and_does_not_spend(tmp_path: Path) -> None:
    clock = _Clock()
    failing = _FailingLedger(path=tmp_path / "effects.ndjson")
    gateway, _, _, epoch, _ = _gateway(tmp_path, clock=clock, ledger=failing)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)

    with pytest.raises(GatewayDenied, match="reservation_failed"):
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert grant.state is GrantState.ACTIVE
    assert grant.claimed_by == attempt.attempt_id
    assert attempt.state is AttemptState.PREPARING


def test_t2_crash_after_reservation_projects_unknown_on_restart(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)

    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert permit.fenced is True
    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED

    restarted = EffectLedger(path=ledger.path)
    item = restarted.recovery_projection()[0]
    assert item.raw_state is EffectState.RESERVED
    assert item.effective_state is EffectState.EFFECT_UNKNOWN
    assert item.unresolved is True


def test_t3_crash_after_submit_leaves_reservation_unresolved(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume_post(gateway, permit, intent)

    # Simulated crash here: no terminal outcome append.
    item = EffectLedger(path=ledger.path).recovery_projection()[0]
    assert permit.consumed is True
    assert item.raw_state is EffectState.RESERVED
    assert item.unresolved is True


def test_t4_timeout_records_effect_unknown_and_blocks_reuse(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume_post(gateway, permit, intent)

    gateway.record_effect_unknown(permit, attempt, evidence={"reason": "timeout"})

    assert attempt.state is AttemptState.EFFECT_UNKNOWN
    item = ledger.recovery_projection()[0]
    assert item.raw_state is EffectState.EFFECT_UNKNOWN
    assert item.unresolved is True
    with pytest.raises(GatewayDenied, match="permit_reused"):
        _consume_post(gateway, permit, intent)


def test_t5_observed_effect_stays_recorded_if_later_verification_fails(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume_post(gateway, permit, intent)

    gateway.record_effect_confirmed(permit, attempt, evidence={"posted_url": "https://x.com/a/status/1"})
    # A later higher-level verification failure cannot erase the ledger fact.
    assert ledger.read_records()[-1].state is EffectState.EFFECT_CONFIRMED
    assert grant.state is GrantState.SPENT


def test_t7_target_mismatch_rejected_without_consuming_permit(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post", target_id="A")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayDenied, match="target_mismatch"):
        gateway.consume_permit(
            permit,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="B",
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.consumed is False


def test_t8_permit_reuse_rejected(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume_post(gateway, permit, intent)

    with pytest.raises(GatewayDenied, match="permit_reused"):
        _consume_post(gateway, permit, intent)


def test_t9_expired_permit_rejected_without_consuming(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    clock.now = permit.expires_at

    with pytest.raises(GatewayDenied, match="permit_expired"):
        _consume_post(gateway, permit, intent)
    assert permit.consumed is False


def test_t10_actor_change_rejected(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayDenied, match="actor_mismatch"):
        gateway.consume_permit(
            permit,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash=intent.intent_hash(),
            actor_id="@other",
            target_type=intent.target_type,
            target_id=intent.target_id,
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.consumed is False


def test_t11_intent_change_rejected(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayDenied, match="intent_mismatch"):
        gateway.consume_permit(
            permit,
            effect=EffectVerb.SUBMIT_CONTENT,
            intent_hash="changed",
            actor_id=intent.actor_identity or "",
            target_type=intent.target_type,
            target_id=intent.target_id,
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.consumed is False


def test_kill_switch_rechecked_at_permit_boundary(tmp_path: Path) -> None:
    gateway, _, kill, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    kill.trip()

    with pytest.raises(GatewayDenied, match="kill_switch"):
        _consume_post(gateway, permit, intent)
    assert permit.consumed is False


def test_epoch_change_invalidates_outstanding_permit(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    epoch.bump()

    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        _consume_post(gateway, permit, intent)
    assert permit.consumed is False


def test_non_fenced_effect_spends_without_reservation(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("bookmark")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)

    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert permit.fenced is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


def test_unknown_non_fenced_outcome_is_written_to_ledger(tmp_path: Path) -> None:
    gateway, ledger, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("bookmark")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SET_BOOKMARK,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type="post",
        target_id="123",
        policy_binding=DEFAULT_EFFECT_POLICIES.require("bookmark").binding_hash(),
    )

    gateway.record_effect_unknown(permit, attempt, evidence={"reason": "selector_timeout"})

    assert attempt.state is AttemptState.EFFECT_UNKNOWN
    assert ledger.recovery_projection()[0].unresolved is True


def test_effect_outside_policy_authority_is_rejected(tmp_path: Path) -> None:
    gateway, _, _, epoch, clock = _gateway(tmp_path)
    intent = _intent("bookmark")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayDenied, match="effect_not_allowed"):
        gateway.consume_permit(
            permit,
            effect=EffectVerb.DELETE_POST,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="123",
            policy_binding=DEFAULT_EFFECT_POLICIES.require("bookmark").binding_hash(),
        )
    assert permit.consumed is False
