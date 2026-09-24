"""Layer-5 regressions for issued-but-unconsumed NO_EFFECT closure."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import (
    CommitGateway,
    GatewayDenied,
    GatewayStateError,
)
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


class _FailNoEffectLedger(EffectLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.fail_no_effect = True

    def append_durable(self, record):  # type: ignore[no-untyped-def]
        if self.fail_no_effect and record.state is EffectState.NO_EFFECT:
            raise EffectLedgerError("injected NO_EFFECT durability failure")
        super().append_durable(record)


def _intent(action: str) -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    payload = {"text": "hello"} if action == "post" else {"post_url": "https://x.com/u/status/123"}
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id="123",
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
    grant = ApprovalGrantStore(clock=clock).mint(
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


def _runtime(
    tmp_path: Path,
    *,
    ledger: EffectLedger | None = None,
) -> tuple[CommitGateway, EffectLedger, AuthorizationEpoch, _Clock]:
    clock = _Clock()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    actual_ledger = ledger or EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=actual_ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=60.0,
    )
    return gateway, actual_ledger, epoch, clock


def _consume(
    gateway: CommitGateway,
    permit,  # type: ignore[no-untyped-def]
    intent: WriteIntent,
    *,
    effect: EffectVerb,
) -> None:
    gateway.consume_permit(
        permit,
        effect=effect,
        intent_hash=intent.intent_hash(),
        actor_id=intent.actor_identity or "",
        target_type=intent.target_type,
        target_id=intent.target_id,
        policy_binding=DEFAULT_EFFECT_POLICIES.require(intent.action_type).binding_hash(),
    )


def test_fenced_unconsumed_closure_persists_no_effect_and_resolves_recovery(
    tmp_path: Path,
) -> None:
    gateway, ledger, epoch, clock = _runtime(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert permit.fenced is True
    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED

    gateway.close_unconsumed_permit_no_effect(
        permit,
        reason="consume_denied:kill_switch",
    )

    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.NO_EFFECT
    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.RESERVED, EffectState.NO_EFFECT]
    terminal = records[-1]
    assert terminal.details["reason"] == "consume_denied:kill_switch"
    assert terminal.details["permit_id"] == permit.permit_id
    assert terminal.details["effect"] is None

    projection = ledger.recovery_projection()
    assert len(projection) == 1
    assert projection[0].effective_state is EffectState.NO_EFFECT
    assert projection[0].unresolved is False

    with pytest.raises(GatewayDenied, match="permit_unknown"):
        _consume(gateway, permit, intent, effect=EffectVerb.SUBMIT_CONTENT)


def test_best_effort_unconsumed_closure_terminalizes_without_ledger_row(
    tmp_path: Path,
) -> None:
    gateway, ledger, epoch, clock = _runtime(tmp_path)
    intent = _intent("bookmark")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert permit.fenced is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []

    gateway.close_unconsumed_permit_no_effect(
        permit,
        reason="consume_denied:epoch_mismatch",
    )

    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.NO_EFFECT
    assert ledger.read_records() == []

    with pytest.raises(GatewayDenied, match="permit_unknown"):
        _consume(gateway, permit, intent, effect=EffectVerb.SET_BOOKMARK)


def test_consumed_permit_cannot_be_closed_as_no_effect(tmp_path: Path) -> None:
    gateway, ledger, epoch, clock = _runtime(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    _consume(gateway, permit, intent, effect=EffectVerb.SUBMIT_CONTENT)

    with pytest.raises(GatewayStateError, match="consumed permit"):
        gateway.close_unconsumed_permit_no_effect(
            permit,
            reason="incorrect_cleanup",
        )

    assert permit.consumed is True
    assert attempt.state is AttemptState.RESERVED
    assert ledger.recovery_projection()[0].unresolved is True

    gateway.record_effect_unknown(
        permit,
        attempt,
        evidence={"reason": "verification_missing"},
    )
    assert attempt.state is AttemptState.EFFECT_UNKNOWN


def test_failed_fenced_closure_preserves_reserved_state_and_can_retry(
    tmp_path: Path,
) -> None:
    ledger = _FailNoEffectLedger(path=tmp_path / "effects.ndjson")
    gateway, _, epoch, clock = _runtime(tmp_path, ledger=ledger)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayDenied, match="no_effect_close_failed"):
        gateway.close_unconsumed_permit_no_effect(
            permit,
            reason="consume_denied:policy_mismatch",
        )

    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED
    projection = ledger.recovery_projection()
    assert len(projection) == 1
    assert projection[0].effective_state is EffectState.EFFECT_UNKNOWN
    assert projection[0].unresolved is True

    ledger.fail_no_effect = False
    gateway.close_unconsumed_permit_no_effect(
        permit,
        reason="consume_denied:policy_mismatch",
    )

    assert attempt.state is AttemptState.NO_EFFECT
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.NO_EFFECT
    assert terminal.details["reason"] == "consume_denied:policy_mismatch"
    assert ledger.recovery_projection()[0].unresolved is False


def test_invalid_closure_reason_does_not_change_issued_permit(tmp_path: Path) -> None:
    gateway, ledger, epoch, clock = _runtime(tmp_path)
    intent = _intent("post")
    grant, attempt = _claimed(intent, epoch=epoch, clock=clock)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    with pytest.raises(GatewayStateError, match="non-empty reason"):
        gateway.close_unconsumed_permit_no_effect(permit, reason="   ")

    assert permit.consumed is False
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED
    assert ledger.read_records()[-1].state is EffectState.RESERVED

    gateway.close_unconsumed_permit_no_effect(permit, reason="manual_abandon")
    assert attempt.state is AttemptState.NO_EFFECT
