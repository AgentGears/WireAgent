"""Concurrency regressions for the M5 Commit Gateway protocol lock."""

from __future__ import annotations

import threading
from pathlib import Path

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _BarrierRLock:
    """RLock-compatible test lock that lines up two competing callers first."""

    def __init__(self) -> None:
        self._barrier = threading.Barrier(2)
        self._lock = threading.RLock()
        self._count_lock = threading.Lock()
        self.entries = 0

    def __enter__(self):  # type: ignore[no-untyped-def]
        with self._count_lock:
            self.entries += 1
        self._barrier.wait(timeout=5)
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):  # type: ignore[no-untyped-def]
        self._lock.release()
        return False


def _intent(action: str = "post") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "concurrency"} if action == "post" else {},
        actor_identity="@actor",
    )


def _claim(intent: WriteIntent, epoch: AuthorizationEpoch):
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


def _consume_args(intent: WriteIntent) -> dict[str, object]:
    return {
        "effect": EffectVerb.SUBMIT_CONTENT,
        "intent_hash": intent.intent_hash(),
        "actor_id": "@actor",
        "target_type": intent.target_type,
        "target_id": intent.target_id,
        "policy_binding": DEFAULT_EFFECT_POLICIES.require(intent.action_type).binding_hash(),
    }


def test_concurrent_consumers_allow_exactly_one_boundary_crossing(tmp_path: Path) -> None:
    """Codex P1: permit use is a true process-local compare-and-set."""
    cfg = WebWireConfig(state_dir=tmp_path)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    test_lock = _BarrierRLock()
    gateway._protocol_lock = test_lock  # type: ignore[assignment]
    results: list[str] = []
    results_lock = threading.Lock()

    def consume() -> None:
        try:
            gateway.consume_permit(permit, **_consume_args(intent))  # type: ignore[arg-type]
            result = "success"
        except GatewayDenied as exc:
            result = exc.reason
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=consume), threading.Thread(target=consume)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert test_lock.entries == 2
    assert sorted(results) == ["permit_reused", "success"]
    assert permit.consumed is True


def test_concurrent_authorize_same_attempt_creates_one_reservation(tmp_path: Path) -> None:
    """The protocol lock also prevents duplicate fences/spends for one attempt."""
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

    test_lock = _BarrierRLock()
    gateway._protocol_lock = test_lock  # type: ignore[assignment]
    results: list[str] = []
    results_lock = threading.Lock()

    def authorize() -> None:
        try:
            gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
            result = "success"
        except GatewayDenied as exc:
            result = exc.reason
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=authorize), threading.Thread(target=authorize)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert test_lock.entries == 2
    assert results.count("success") == 1
    assert len(results) == 2
    assert set(results) <= {"success", "attempt_not_preparing", "grant_not_active"}
    reserved = [record for record in ledger.read_records() if record.state is EffectState.RESERVED]
    assert len(reserved) == 1


def test_concurrent_terminal_outcomes_record_only_one_terminal_fact(tmp_path: Path) -> None:
    """Confirmed/unknown cannot race into conflicting durable terminal facts."""
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
    gateway.consume_permit(permit, **_consume_args(intent))  # type: ignore[arg-type]

    test_lock = _BarrierRLock()
    gateway._protocol_lock = test_lock  # type: ignore[assignment]
    results: list[str] = []
    results_lock = threading.Lock()

    def record_confirmed() -> None:
        try:
            gateway.record_effect_confirmed(permit, attempt, evidence={"source": "confirmed"})
            result = "confirmed"
        except GatewayDenied as exc:
            result = exc.reason
        with results_lock:
            results.append(result)

    def record_unknown() -> None:
        try:
            gateway.record_effect_unknown(permit, attempt, evidence={"source": "unknown"})
            result = "unknown"
        except GatewayDenied as exc:
            result = exc.reason
        with results_lock:
            results.append(result)

    threads = [threading.Thread(target=record_confirmed), threading.Thread(target=record_unknown)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert test_lock.entries == 2
    assert len(results) == 2
    assert results.count("permit_reused") == 1
    assert (results.count("confirmed") + results.count("unknown")) == 1
    terminal = [
        record
        for record in ledger.read_records()
        if record.state in {EffectState.EFFECT_CONFIRMED, EffectState.EFFECT_UNKNOWN}
    ]
    assert len(terminal) == 1
