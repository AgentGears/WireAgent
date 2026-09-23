"""Regressions for the final Codex review of M5 layer 3."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied, GatewayStateError
from webwire.safety.effect_ledger import EffectLedger, EffectLedgerError, EffectState
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
)
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _Clock:
    def __init__(self, now: float = 12000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _FailExpiryCloseLedger(EffectLedger):
    """Allow RESERVED, fail the first attempted terminal closure."""

    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self.fail_close = True
        self.calls = 0

    def append_durable(self, record):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.fail_close and record.state is EffectState.NO_EFFECT:
            raise EffectLedgerError("injected expiry close failure")
        return super().append_durable(record)


def _intent(action: str = "post", target_id: str = "123") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "final review"} if action == "post" else {},
        actor_identity="@actor",
    )


def _claim(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
    *,
    clock: _Clock | None = None,
    policy_binding: str | None = None,
):
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
    store = ApprovalGrantStore(clock=clock or _Clock())
    binding = policy_binding or policy.binding_hash()
    grant = store.mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type=intent.action_type,
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=binding,
        authorization_epoch=epoch.current,
    )
    attempt = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        attempt.attempt_id,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        policy_binding=binding,
        authorization_epoch=epoch.current,
        now=(clock() if clock is not None else None),
    )
    return grant, attempt


def _consume_post(
    gateway: CommitGateway,
    permit,
    intent: WriteIntent,
    *,
    policy_binding: str,
) -> None:  # type: ignore[no-untyped-def]
    gateway.consume_permit(
        permit,
        effect=EffectVerb.SUBMIT_CONTENT,
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=policy_binding,
    )


def test_expired_fenced_permit_closes_reserved_with_no_effect(tmp_path: Path) -> None:
    """Codex P2: expiry cannot leave a known-never-consumed RESERVED orphan."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=5.0,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert permit.fenced is True

    clock.now = permit.expires_at + 1
    with pytest.raises(GatewayDenied, match="permit_expired"):
        _consume_post(
            gateway,
            permit,
            intent,
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )

    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.RESERVED, EffectState.NO_EFFECT]
    projection = ledger.recovery_projection()
    assert len(projection) == 1
    assert projection[0].raw_state is EffectState.NO_EFFECT
    assert projection[0].unresolved is False
    assert permit.permit_id not in gateway._issued_permits


def test_failed_expiry_close_retains_permit_until_no_effect_is_durable(tmp_path: Path) -> None:
    """If NO_EFFECT persistence fails, keep the handle so closure can retry."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = _FailExpiryCloseLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=5.0,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    clock.now = permit.expires_at + 1

    with pytest.raises(GatewayDenied, match="expiry_close_failed"):
        _consume_post(
            gateway,
            permit,
            intent,
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.permit_id in gateway._issued_permits
    assert ledger.recovery_projection()[0].unresolved is True

    ledger.fail_close = False
    with pytest.raises(GatewayDenied, match="permit_expired"):
        _consume_post(
            gateway,
            permit,
            intent,
            policy_binding=DEFAULT_EFFECT_POLICIES.require("post").binding_hash(),
        )
    assert permit.permit_id not in gateway._issued_permits
    assert ledger.recovery_projection()[0].unresolved is False


def test_opportunistic_prune_durably_closes_expired_fenced_permit(tmp_path: Path) -> None:
    """The authorize-time pruning path has the same NO_EFFECT obligation."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=5.0,
    )

    first = _intent("post", "first")
    grant1, attempt1 = _claim(first, epoch, clock=clock)
    permit1 = gateway.authorize_commit(grant=grant1, attempt=attempt1, intent=first)
    clock.now = permit1.expires_at + 1

    second = _intent("post", "second")
    grant2, attempt2 = _claim(second, epoch, clock=clock)
    gateway.authorize_commit(grant=grant2, attempt=attempt2, intent=second)

    states_by_effect: dict[str, list[EffectState]] = {}
    for record in ledger.read_records():
        states_by_effect.setdefault(record.effect_id, []).append(record.state)
    assert states_by_effect[permit1.effect_id] == [EffectState.RESERVED, EffectState.NO_EFFECT]
    assert permit1.permit_id not in gateway._issued_permits


def test_policy_registration_cannot_linearize_inside_permit_consume(tmp_path: Path) -> None:
    """Codex P1: registry replacement and consumption share policy_fence()."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    p1 = DEFAULT_EFFECT_POLICIES.require("post")
    policies = EffectPolicyRegistry()
    policies.register(p1)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        policies=policies,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch, policy_binding=p1.binding_hash())
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    p2 = EffectPolicy.derive(
        action_type="post",
        risk_tier=p1.risk_tier,
        allowed_effects={EffectVerb.SUBMIT_CONTENT},
        replay_semantics=p1.replay_semantics,
    )
    assert p2.binding_hash() != p1.binding_hash()

    inside_policy_check = threading.Event()
    release_consume = threading.Event()
    register_started = threading.Event()
    register_done = threading.Event()
    consume_done = threading.Event()
    original_binding = gateway._current_policy_binding

    def blocked_binding(action_type: str) -> str:
        inside_policy_check.set()
        assert release_consume.wait(timeout=5)
        return original_binding(action_type)

    gateway._current_policy_binding = blocked_binding  # type: ignore[method-assign]

    def consume() -> None:
        _consume_post(gateway, permit, intent, policy_binding=p1.binding_hash())
        consume_done.set()

    def replace_policy() -> None:
        register_started.set()
        policies.register(p2)
        register_done.set()

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert inside_policy_check.wait(timeout=5)

    writer = threading.Thread(target=replace_policy)
    writer.start()
    assert register_started.wait(timeout=5)
    assert register_done.is_set() is False

    release_consume.set()
    consumer.join(timeout=10)
    writer.join(timeout=10)
    assert not consumer.is_alive()
    assert not writer.is_alive()
    assert consume_done.is_set() is True
    assert permit.consumed is True
    assert register_done.is_set() is True
    assert policies.require("post").binding_hash() == p2.binding_hash()


def test_failing_kill_listener_cannot_suppress_epoch_listener(tmp_path: Path) -> None:
    """Codex P1: listener failures are isolated and later listeners still run."""
    cfg = WebWireConfig(state_dir=tmp_path)
    kill = KillSwitch(cfg)
    calls = 0

    def failing_listener() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("injected listener failure")

    kill.add_trip_listener(failing_listener)
    epoch = AuthorizationEpoch()
    CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )

    kill.trip()
    assert epoch.current == 1
    assert calls == 1

    # The failing listener remains retryable during this active trip; the
    # already-successful epoch listener is not re-fired.
    assert kill.tripped() is True
    assert calls == 2
    assert epoch.current == 1


@pytest.mark.parametrize("reserved_key", ["attempt_id", "grant_id", "permit_id", "effect"])
def test_terminal_evidence_cannot_override_gateway_owned_fields(
    tmp_path: Path,
    reserved_key: str,
) -> None:
    """Codex P2: caller evidence cannot forge gateway correlation metadata."""
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
    binding = DEFAULT_EFFECT_POLICIES.require("post").binding_hash()
    _consume_post(gateway, permit, intent, policy_binding=binding)

    with pytest.raises(GatewayStateError, match="reserved ledger detail keys"):
        gateway.record_effect_confirmed(
            permit,
            attempt,
            evidence={reserved_key: "forged"},
        )

    assert attempt.state is AttemptState.RESERVED
    assert permit.permit_id in gateway._issued_permits
    assert [record.state for record in ledger.read_records()] == [EffectState.RESERVED]

    gateway.record_effect_confirmed(permit, attempt, evidence={"source": "valid"})
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.EFFECT_CONFIRMED
    assert terminal.details["attempt_id"] == attempt.attempt_id
    assert terminal.details["permit_id"] == permit.permit_id
    assert terminal.details["effect"] == EffectVerb.SUBMIT_CONTENT.value
