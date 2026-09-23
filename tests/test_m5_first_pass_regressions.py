"""Regressions from the maintainer-first review of M5 layer 3."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied, GatewayStateError
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    EffectVerb,
    ReplaySemantics,
)
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantClaimDenied,
    GrantState,
    GrantStateError,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float = 20_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _BlockingReservationLedger(EffectLedger):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self.entered = threading.Event()
        self.release = threading.Event()

    def append_durable(self, record):  # type: ignore[no-untyped-def]
        if record.state is EffectState.RESERVED:
            self.entered.set()
            assert self.release.wait(timeout=5)
        return super().append_durable(record)


def _intent(action: str = "post", target_id: str = "123") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    payload = {"text": "first-pass"} if action == "post" else {}
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        payload=payload,
        actor_identity="@actor",
    )


def _claim(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
    *,
    clock: _Clock | None = None,
):
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
    store = ApprovalGrantStore(clock=clock or _Clock())
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
        now=(clock() if clock is not None else None),
    )
    return grant, attempt


def _consume(
    gateway: CommitGateway,
    permit,
    intent: WriteIntent,
    effect: EffectVerb,
) -> None:  # type: ignore[no-untyped-def]
    gateway.consume_permit(
        permit,
        effect=effect,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=permit.policy_binding,
    )


def test_pending_critical_revocation_blocks_after_reset_before_epoch_delivery(
    tmp_path: Path,
) -> None:
    """F1: clearing live kill state cannot reopen pre-trip authority."""
    cfg = WebWireConfig(state_dir=tmp_path)
    kill = KillSwitch(cfg)
    listener_entered = threading.Event()
    release_listener = threading.Event()

    def reset_then_block() -> None:
        kill.reset()
        listener_entered.set()
        assert release_listener.wait(timeout=5)

    # Deliberately before the gateway's critical epoch listener.
    kill.add_trip_listener(reset_then_block)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    trip_thread = threading.Thread(target=kill.trip)
    trip_thread.start()
    assert listener_entered.wait(timeout=5)
    assert kill.state()["tripped"] is False
    assert epoch.current == 0
    assert kill.state()["critical_revocation_pending"] is True

    # The critical generation is still owed, so authority remains closed even
    # though the first callback reset the visible kill flag.
    with pytest.raises(GatewayDenied, match="kill_switch"):
        _consume(gateway, permit, intent, EffectVerb.SUBMIT_CONTENT)
    assert permit.consumed is False

    release_listener.set()
    trip_thread.join(timeout=10)
    assert not trip_thread.is_alive()
    assert epoch.current == 1

    # Once revocation delivery completes, denial comes from epoch lineage.
    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        _consume(gateway, permit, intent, EffectVerb.SUBMIT_CONTENT)


def test_expired_fenced_permit_terminalizes_canonical_attempt(tmp_path: Path) -> None:
    """F2: durable NO_EFFECT and live attempt must converge on the same fact."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=5.0,
    )
    intent = _intent("post")
    grant, attempt = _claim(intent, epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert attempt.state is AttemptState.RESERVED
    assert grant.state is GrantState.SPENT

    clock.now = permit.expires_at
    with pytest.raises(GatewayDenied, match="permit_expired"):
        _consume(gateway, permit, intent, EffectVerb.SUBMIT_CONTENT)

    assert attempt.state is AttemptState.NO_EFFECT
    assert grant.state is GrantState.SPENT
    assert permit.permit_id not in gateway._issued_permits
    assert permit.permit_id not in gateway._issued_attempts


def test_expired_non_fenced_permit_terminalizes_canonical_attempt(tmp_path: Path) -> None:
    """The same post-authority NO_EFFECT transition applies without RESERVED."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=5.0,
    )
    intent = _intent("bookmark")
    grant, attempt = _claim(intent, epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert permit.fenced is False
    assert attempt.state is AttemptState.PREPARING
    assert grant.state is GrantState.SPENT

    clock.now = permit.expires_at
    with pytest.raises(GatewayDenied, match="permit_expired"):
        _consume(gateway, permit, intent, EffectVerb.SET_BOOKMARK)

    assert attempt.state is AttemptState.NO_EFFECT
    assert grant.state is GrantState.SPENT


def test_outcome_rejects_reconstructed_attempt_even_with_matching_lineage(
    tmp_path: Path,
) -> None:
    """F3: ids are correlation metadata, not authority to replace the object."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume(gateway, permit, intent, EffectVerb.SUBMIT_CONTENT)

    forged = EffectAttempt(
        grant_id=attempt.grant_id,
        attempt_id=attempt.attempt_id,
        state=attempt.state,
    )
    with pytest.raises(GatewayStateError, match="canonical EffectAttempt"):
        gateway.record_effect_confirmed(permit, forged)

    assert attempt.state is AttemptState.RESERVED
    assert permit.permit_id in gateway._issued_permits
    gateway.record_effect_confirmed(permit, attempt)
    assert attempt.state is AttemptState.EFFECT_CONFIRMED


def test_gateway_holds_grant_claim_fence_through_reservation_and_spend(
    tmp_path: Path,
) -> None:
    """F4: a concurrent clean-failure release cannot race the commit protocol."""
    cfg = WebWireConfig(state_dir=tmp_path)
    ledger = _BlockingReservationLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    authorize_result: list[object] = []
    clean_result: list[object] = []
    clean_started = threading.Event()
    clean_done = threading.Event()

    def authorize() -> None:
        try:
            authorize_result.append(
                gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
            )
        except Exception as exc:  # pragma: no cover - asserted below
            authorize_result.append(exc)

    def clean_failure() -> None:
        clean_started.set()
        try:
            attempt.mark_no_effect(grant)
            clean_result.append("released")
        except Exception as exc:
            clean_result.append(exc)
        finally:
            clean_done.set()

    authorize_thread = threading.Thread(target=authorize)
    authorize_thread.start()
    assert ledger.entered.wait(timeout=5)

    clean_thread = threading.Thread(target=clean_failure)
    clean_thread.start()
    assert clean_started.wait(timeout=5)
    assert clean_done.wait(timeout=0.1) is False

    ledger.release.set()
    authorize_thread.join(timeout=10)
    clean_thread.join(timeout=10)
    assert not authorize_thread.is_alive()
    assert not clean_thread.is_alive()

    assert len(authorize_result) == 1
    assert not isinstance(authorize_result[0], Exception)
    assert len(clean_result) == 1
    assert isinstance(clean_result[0], GrantStateError)
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED


def test_concurrent_claims_have_exactly_one_owner() -> None:
    """ApprovalGrant.claim is an actual CAS, not a sequential convention."""
    epoch = AuthorizationEpoch()
    intent = _intent()
    grant, first_attempt = _claim(intent, epoch)
    # Release the helper-created claim so two fresh attempts can contend.
    first_attempt.mark_no_effect(grant)

    attempts = [EffectAttempt(grant_id=grant.grant_id) for _ in range(2)]
    barrier = threading.Barrier(3)
    results: list[str] = []
    results_lock = threading.Lock()
    policy = DEFAULT_EFFECT_POLICIES.require("post")

    def claim(attempt: EffectAttempt) -> None:
        barrier.wait(timeout=5)
        try:
            grant.claim(
                attempt.attempt_id,
                intent_hash=intent.intent_hash(),
                actor_id="@actor",
                policy_binding=policy.binding_hash(),
                authorization_epoch=epoch.current,
            )
            result = "success"
        except GrantClaimDenied as exc:
            result = exc.reason
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=claim, args=(attempt,)) for attempt in attempts]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=5)
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert results.count("success") == 1
    assert results.count("approval_already_claimed") == 1
    assert grant.claimed_by in {attempt.attempt_id for attempt in attempts}


def test_unimplemented_future_actions_remain_conservatively_fenced() -> None:
    """F6: no real broker implementation means replay semantics stay UNKNOWN."""
    for action in ("follow", "unfollow", "repost", "unrepost"):
        policy = DEFAULT_EFFECT_POLICIES.require(action)
        assert policy.replay_semantics is ReplaySemantics.UNKNOWN
        assert policy.durability is DurabilityPolicy.REQUIRED
