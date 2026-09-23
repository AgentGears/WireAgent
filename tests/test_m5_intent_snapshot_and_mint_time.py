"""Regressions for intent snapshot and mint-time authority in M5 layer 3."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
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
    def __init__(self, now: float = 10_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _BlockingLedger(EffectLedger):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self.entered = threading.Event()
        self.release = threading.Event()

    def append_durable(self, record: EffectLedgerRecord) -> None:
        if record.state is EffectState.RESERVED:
            self.entered.set()
            assert self.release.wait(timeout=5)
        super().append_durable(record)


class _AdvancingLedger(EffectLedger):
    def __init__(
        self,
        config: WebWireConfig,
        clock: _Clock,
        advance_seconds: float,
        *,
        fail_no_effect: bool = False,
    ) -> None:
        super().__init__(config)
        self._clock = clock
        self._advance = advance_seconds
        self._fail_no_effect = fail_no_effect
        self._advanced = False

    def append_durable(self, record: EffectLedgerRecord) -> None:
        if record.state is EffectState.RESERVED and not self._advanced:
            super().append_durable(record)
            self._clock.now += self._advance
            self._advanced = True
            return
        if record.state is EffectState.NO_EFFECT and self._fail_no_effect:
            raise EffectLedgerError("injected pre-permit close failure")
        super().append_durable(record)


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "approved"},
        actor_identity="@actor",
    )


def _claimed(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
    clock: _Clock,
    *,
    ttl_seconds: float = 300.0,
) -> tuple[ApprovalGrant, EffectAttempt]:
    policy = DEFAULT_EFFECT_POLICIES.require("post")
    store = ApprovalGrantStore(clock=clock, ttl_seconds=ttl_seconds)
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
    cfg: WebWireConfig,
    ledger: EffectLedger,
    epoch: AuthorizationEpoch,
    clock: _Clock,
    *,
    permit_ttl: float = 30.0,
) -> CommitGateway:
    return CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        clock=clock,
        permit_ttl_seconds=permit_ttl,
    )


def test_authorize_uses_one_snapshot_if_caller_mutates_intent_during_fsync(
    tmp_path: Path,
) -> None:
    """Codex P1: validation A can never mint authority for later intent B."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = _BlockingLedger(cfg)
    gateway = _gateway(cfg, ledger, epoch, clock)
    intent = _intent()
    approved_hash = intent.intent_hash()
    approved_key = intent.dedupe_key()
    grant, attempt = _claimed(intent, epoch, clock)
    result: list[object] = []

    def authorize() -> None:
        try:
            result.append(
                gateway.authorize_commit(
                    grant=grant,
                    attempt=attempt,
                    intent=intent,
                )
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result.append(exc)

    thread = threading.Thread(target=authorize)
    thread.start()
    assert ledger.entered.wait(timeout=5)

    # The caller-owned object is deliberately changed after grant validation
    # and while REQUIRED durability is blocked.
    intent.payload["text"] = "tampered"
    intent.actor_identity = "@other"
    intent.target_id = "999"
    assert intent.intent_hash() != approved_hash

    ledger.release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(result) == 1
    assert not isinstance(result[0], Exception)

    permit = result[0]
    assert permit.intent_hash == approved_hash  # type: ignore[attr-defined]
    assert permit.semantic_key == approved_key  # type: ignore[attr-defined]
    assert permit.actor_id == "@actor"  # type: ignore[attr-defined]
    assert permit.target_id == "123"  # type: ignore[attr-defined]

    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].intent_hash == approved_hash
    assert records[0].semantic_key == approved_key
    assert records[0].actor_id == "@actor"
    assert records[0].target_id == "123"


def test_permit_ttl_starts_when_permit_is_actually_minted(tmp_path: Path) -> None:
    """Codex P2: reservation latency must not consume the new permit's TTL."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = _AdvancingLedger(cfg, clock, advance_seconds=20.0)
    gateway = _gateway(cfg, ledger, epoch, clock, permit_ttl=5.0)
    intent = _intent()
    grant, attempt = _claimed(intent, epoch, clock)

    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert permit.issued_at == 10_020.0
    assert permit.expires_at == 10_025.0
    assert permit.expires_at > clock()
    assert grant.state is GrantState.SPENT
    assert attempt.state is AttemptState.RESERVED


def test_grant_expiry_during_required_reservation_closes_without_minting(
    tmp_path: Path,
) -> None:
    """Post-freeze maintainer finding: approval must still be live at mint."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = _AdvancingLedger(cfg, clock, advance_seconds=10.0)
    gateway = _gateway(cfg, ledger, epoch, clock)
    intent = _intent()
    grant, attempt = _claimed(intent, epoch, clock, ttl_seconds=5.0)

    with pytest.raises(GatewayDenied) as denied:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert denied.value.reason == "expired"

    assert grant.state is GrantState.EXPIRED
    assert attempt.state is AttemptState.NO_EFFECT
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]
    assert ledger.recovery_projection()[0].unresolved is False


def test_failed_prepermit_close_leaves_reservation_unresolved_and_no_authority(
    tmp_path: Path,
) -> None:
    """Closure I/O failure stays conservative: unresolved, never reopened."""
    cfg = WebWireConfig(state_dir=tmp_path)
    clock = _Clock()
    epoch = AuthorizationEpoch()
    ledger = _AdvancingLedger(
        cfg,
        clock,
        advance_seconds=10.0,
        fail_no_effect=True,
    )
    gateway = _gateway(cfg, ledger, epoch, clock)
    intent = _intent()
    grant, attempt = _claimed(intent, epoch, clock, ttl_seconds=5.0)

    with pytest.raises(GatewayDenied) as denied:
        gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert denied.value.reason == "prepermit_close_failed"

    assert grant.state is GrantState.EXPIRED
    assert attempt.state is AttemptState.RESERVED
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}
    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.RESERVED]
    assert ledger.recovery_projection()[0].unresolved is True
