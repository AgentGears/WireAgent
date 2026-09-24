"""Layer-5 execution-runtime lifecycle and receipt integration tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    GrantState,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_execution_runtime import (
    M5ExecutionDenied,
    M5ExecutionRuntime,
    M5ExecutionStateError,
)
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker


class _BookmarkBroker:
    def __init__(self, *, fail_before_gate: bool = False) -> None:
        self.fail_before_gate = fail_before_gate
        self.urls: list[str] = []
        self.gate_calls = 0

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        self.urls.append(post_url)
        if self.fail_before_gate:
            return soft_failure(
                "probe failed before commit",
                failure_category=FailureCategory.TIMEOUT,
            )
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"bookmarked": True})


class _LikeBroker:
    def __init__(self) -> None:
        self.gate_calls = 0

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"liked": True, "post_url": post_url})


class _KillBeforeConsumeGateway(CommitGateway):
    def __init__(self, *, kill_switch: KillSwitch, **kwargs: Any) -> None:
        super().__init__(kill_switch=kill_switch, **kwargs)
        self._test_kill = kill_switch
        self._trip_before_consume = True

    def consume_permit(self, permit, **kwargs):  # type: ignore[no-untyped-def]
        if self._trip_before_consume:
            self._trip_before_consume = False
            self._test_kill.trip()
        return super().consume_permit(permit, **kwargs)


def _intent(action: str, *, actor: str | None = "@actor") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"post_url": "https://x.com/u/status/123"},
        actor_identity=actor,
    )


def _runtime(
    tmp_path: Path,
    broker: Any,
    *,
    grants: ApprovalGrantStore | None = None,
    kill_before_consume: bool = False,
) -> tuple[M5ExecutionRuntime, CommitGateway, EffectLedger, KillSwitch]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    ledger = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway_cls = _KillBeforeConsumeGateway if kill_before_consume else CommitGateway
    gateway = gateway_cls(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(
        broker,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        grants=grants,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    return runtime, gateway, ledger, kill


async def test_issue_freezes_confirmed_intent_before_caller_mutation(
    tmp_path: Path,
) -> None:
    broker = _BookmarkBroker()
    runtime, _, _, _ = _runtime(tmp_path, broker)
    intent = _intent("bookmark")
    original_hash = intent.intent_hash()
    session = runtime.issue(intent)

    intent.target_id = "999"
    intent.payload["post_url"] = "https://x.com/u/status/999"

    receipt = session.scope_effect()
    result = await receipt.authority.apply()

    assert result.ok is True
    assert session.intent_hash == original_hash
    assert broker.urls == ["https://x.com/u/status/123"]
    assert receipt.permit is not None
    assert receipt.permit.target_id == "123"


async def test_scoped_handle_without_permit_allows_clean_precommit_retry(
    tmp_path: Path,
) -> None:
    broker = _BookmarkBroker(fail_before_gate=True)
    runtime, _, ledger, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("bookmark"))
    first_attempt = session.attempt

    receipt = session.scope_effect()
    result = await receipt.authority.apply()

    assert result.ok is False
    assert receipt.permit is None
    assert session.grant.state is GrantState.ACTIVE
    assert session.grant.claimed_by == first_attempt.attempt_id

    session.resolve_no_external_effect(reason="probe_failed")

    assert first_attempt.state is AttemptState.NO_EFFECT
    assert session.grant.state is GrantState.ACTIVE
    assert session.grant.claimed_by is None
    assert session.grant.precommit_attempts == 1
    assert ledger.read_records() == []

    second_attempt = session.retry_clean_precommit()

    assert second_attempt is session.attempt
    assert second_attempt is not first_attempt
    assert second_attempt.state is AttemptState.PREPARING
    assert session.grant.claimed_by == second_attempt.attempt_id
    assert session.authorized_effect is None


def test_clean_precommit_retry_budget_is_bounded(tmp_path: Path) -> None:
    broker = _BookmarkBroker()
    grants = ApprovalGrantStore(max_precommit_attempts=2)
    runtime, _, _, _ = _runtime(tmp_path, broker, grants=grants)
    session = runtime.issue(_intent("bookmark"))

    session.resolve_no_external_effect(reason="attempt_1_clean_failure")
    session.retry_clean_precommit()
    session.resolve_no_external_effect(reason="attempt_2_clean_failure")

    assert session.grant.precommit_attempts == 2
    assert session.grant.state is GrantState.ACTIVE
    with pytest.raises(M5ExecutionDenied) as exc_info:
        session.retry_clean_precommit()
    assert exc_info.value.reason == "attempts_exhausted"


async def test_kill_denied_consume_is_closed_no_effect_by_runtime(
    tmp_path: Path,
) -> None:
    broker = _LikeBroker()
    runtime, _, ledger, kill = _runtime(
        tmp_path,
        broker,
        kill_before_consume=True,
    )
    session = runtime.issue(_intent("like"))
    receipt = session.scope_effect()

    result = await receipt.authority.apply()

    assert result.ok is False
    assert kill.tripped() is True
    assert receipt.permit is not None
    assert receipt.permit.consumed is False
    assert session.grant.state is GrantState.SPENT
    assert session.attempt.state is AttemptState.RESERVED
    assert ledger.recovery_projection()[0].unresolved is True

    session.resolve_no_external_effect(reason="consume_denied:kill_switch")

    assert session.attempt.state is AttemptState.NO_EFFECT
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    assert ledger.recovery_projection()[0].unresolved is False
    with pytest.raises(M5ExecutionStateError, match="commit authority was issued"):
        session.retry_clean_precommit()


async def test_consumed_receipt_can_record_confirmed_effect(tmp_path: Path) -> None:
    broker = _BookmarkBroker()
    runtime, _, ledger, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("bookmark"))
    receipt = session.scope_effect()

    result = await receipt.authority.apply()
    assert result.ok is True
    assert receipt.permit is not None
    assert receipt.permit.consumed is True

    session.record_confirmed(evidence={"verified_state": "bookmarked"})

    assert session.attempt.state is AttemptState.EFFECT_CONFIRMED
    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].state is EffectState.EFFECT_CONFIRMED
    assert records[0].details["verified_state"] == "bookmarked"


async def test_consumed_receipt_can_record_unknown_effect(tmp_path: Path) -> None:
    broker = _LikeBroker()
    runtime, _, ledger, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("like"))
    receipt = session.scope_effect()

    result = await receipt.authority.apply()
    assert result.ok is True
    assert receipt.permit is not None
    assert receipt.permit.consumed is True

    session.record_unknown(evidence={"reason": "verification_missing"})

    assert session.attempt.state is AttemptState.EFFECT_UNKNOWN
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    assert records[-1].details["reason"] == "verification_missing"


async def test_consumed_permit_cannot_be_resolved_as_no_effect(tmp_path: Path) -> None:
    broker = _BookmarkBroker()
    runtime, _, _, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("bookmark"))
    receipt = session.scope_effect()

    result = await receipt.authority.apply()
    assert result.ok is True
    assert receipt.permit is not None and receipt.permit.consumed

    with pytest.raises(M5ExecutionStateError, match="cannot be resolved as NO_EFFECT"):
        session.resolve_no_external_effect(reason="incorrect_cleanup")


def test_reservation_started_without_permit_remains_unresolved(tmp_path: Path) -> None:
    broker = _LikeBroker()
    runtime, _, ledger, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("like"))

    session.attempt.begin_reservation(session.grant)

    with pytest.raises(M5ExecutionStateError, match="state is unresolved"):
        session.resolve_no_external_effect(reason="ambiguous_reservation")

    assert session.attempt.state is AttemptState.PREPARING
    assert session.attempt.reservation_started is True
    assert session.grant.state is GrantState.ACTIVE
    assert session.grant.claimed_by == session.attempt.attempt_id
    assert ledger.read_records() == []


def test_issue_requires_actor_identity(tmp_path: Path) -> None:
    broker = _BookmarkBroker()
    runtime, _, _, _ = _runtime(tmp_path, broker)

    with pytest.raises(M5ExecutionDenied) as exc_info:
        runtime.issue(_intent("bookmark", actor=None))
    assert exc_info.value.reason == "actor_missing"


def test_scope_effect_is_stable_for_one_attempt(tmp_path: Path) -> None:
    broker = _BookmarkBroker()
    runtime, _, _, _ = _runtime(tmp_path, broker)
    session = runtime.issue(_intent("bookmark"))

    first = session.scope_effect()
    second = session.scope_effect()

    assert second is first
    assert first.attempt is session.attempt
