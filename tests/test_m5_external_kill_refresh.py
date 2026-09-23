"""External hot-file kill refresh regressions for M5 layer 3.

An external process cannot participate in the in-process KillSwitch lock.  These
regressions therefore prove the supported contract: if the hot file appears
while an authority path is blocked in slow work, CommitGateway re-observes it
at the final mint/consume boundary and refuses authority.  They deliberately do
not claim cross-process atomicity for a file created after the final observation.
"""

from __future__ import annotations

import threading
from pathlib import Path

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger, EffectLedgerRecord, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _BlockingReservationLedger(EffectLedger):
    """Pause REQUIRED reservation after gateway preflight, before disk append."""

    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self.entered = threading.Event()
        self.release = threading.Event()

    def append_durable(self, record: EffectLedgerRecord) -> None:
        if record.state is EffectState.RESERVED:
            self.entered.set()
            assert self.release.wait(timeout=5)
        super().append_durable(record)


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"text": "external kill refresh"},
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


def test_hot_file_created_during_required_reservation_blocks_permit_mint(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    epoch = AuthorizationEpoch()
    kill = KillSwitch(cfg)
    ledger = _BlockingReservationLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
    )
    intent = _intent()
    grant, attempt = _claim(intent, epoch)
    result: list[object] = []

    def authorize() -> None:
        try:
            result.append(
                gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
            )
        except Exception as exc:  # pragma: no cover - asserted below
            result.append(exc)

    thread = threading.Thread(target=authorize)
    thread.start()
    assert ledger.entered.wait(timeout=5)

    # Simulate an external operator/process.  It does not acquire KillSwitch's
    # Python lock, so the old entry-only observation could miss this indefinitely.
    cfg.kill_path().parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_path().touch()
    ledger.release.set()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], GatewayDenied)
    assert result[0].reason == "kill_switch"
    assert gateway._issued_permits == {}
    assert gateway._issued_attempts == {}
    assert attempt.state is AttemptState.NO_EFFECT
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]

    # Final refresh publishes the trip generation but intentionally runs no
    # callback under the authority fence.  Normal observation drains it later.
    assert epoch.current == 0
    assert kill.tripped() is True
    assert epoch.current == 1


def test_hot_file_created_during_consume_wait_blocks_permit_consumption(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
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
    binding = DEFAULT_EFFECT_POLICIES.require("post").binding_hash()

    inside_consume = threading.Event()
    release_consume = threading.Event()
    result: list[object] = []
    original_binding = gateway._current_policy_binding

    def blocked_binding(action_type: str) -> str:
        inside_consume.set()
        assert release_consume.wait(timeout=5)
        return original_binding(action_type)

    gateway._current_policy_binding = blocked_binding  # type: ignore[method-assign]

    def consume() -> None:
        try:
            gateway.consume_permit(
                permit,
                effect=EffectVerb.SUBMIT_CONTENT,
                intent_hash=intent.intent_hash(),
                actor_id="@actor",
                target_type="post",
                target_id="123",
                policy_binding=binding,
            )
            result.append("consumed")
        except Exception as exc:  # pragma: no cover - asserted below
            result.append(exc)

    thread = threading.Thread(target=consume)
    thread.start()
    assert inside_consume.wait(timeout=5)

    cfg.kill_path().parent.mkdir(parents=True, exist_ok=True)
    cfg.kill_path().touch()
    release_consume.set()

    thread.join(timeout=10)
    assert not thread.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], GatewayDenied)
    assert result[0].reason == "kill_switch"
    assert permit.consumed is False
    assert attempt.state is AttemptState.RESERVED

    assert epoch.current == 0
    assert kill.tripped() is True
    assert epoch.current == 1
