"""Maintainer regressions for direct authorization-epoch linearization.

Kill-triggered epoch bumps already share the kill execution fence. These tests
cover the independent API contract: a caller may advance AuthorizationEpoch
directly, and that revocation must linearize with final permit mint/consume
rather than race a sampled integer.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway, GatewayDenied
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectVerb
from webwire.safety.execution_models import (
    ApprovalGrant,
    ApprovalGrantStore,
    AuthorizationEpoch,
    EffectAttempt,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY


class _PausingEpoch(AuthorizationEpoch):
    """Expose a deterministic pause while the real epoch fence is held."""

    def __init__(self) -> None:
        super().__init__()
        self.pause = False
        self.entered = threading.Event()
        self.release = threading.Event()

    @contextmanager
    def fence(self) -> Iterator[int]:
        with super().fence() as current:
            if self.pause:
                self.entered.set()
                assert self.release.wait(timeout=5)
            yield current


def _bookmark_intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("bookmark")
    return WriteIntent(
        action_type="bookmark",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        actor_identity="@actor",
        payload={"post_url": "https://x.com/a/status/123", "post_id": "123"},
    )


def _claimed(intent: WriteIntent, epoch: AuthorizationEpoch) -> tuple[ApprovalGrant, EffectAttempt]:
    policy = DEFAULT_EFFECT_POLICIES.require(intent.action_type)
    grant = ApprovalGrantStore().mint(
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
    )
    return grant, attempt


def _gateway(tmp_path: Path, epoch: AuthorizationEpoch) -> CommitGateway:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
    )


def test_direct_epoch_bump_linearizes_after_final_permit_mint(tmp_path: Path) -> None:
    """A bump cannot slip between final epoch validation and permit mint."""
    epoch = _PausingEpoch()
    gateway = _gateway(tmp_path, epoch)
    intent = _bookmark_intent()
    grant, attempt = _claimed(intent, epoch)
    result: list[object] = []

    epoch.pause = True

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

    authorizer = threading.Thread(target=authorize)
    authorizer.start()
    assert epoch.entered.wait(timeout=5)

    bump_done = threading.Event()

    def bump() -> None:
        epoch.bump()
        bump_done.set()

    bumper = threading.Thread(target=bump)
    bumper.start()
    assert not bump_done.is_set()

    epoch.release.set()
    authorizer.join(timeout=10)
    bumper.join(timeout=10)
    assert not authorizer.is_alive()
    assert not bumper.is_alive()
    assert len(result) == 1
    assert not isinstance(result[0], Exception)

    permit = result[0]
    assert permit.authorization_epoch == 0  # type: ignore[attr-defined]
    assert epoch.current == 1

    # The permit crossed mint before revocation, but is stale immediately after
    # the bump and therefore cannot cross the later mutation boundary.
    with pytest.raises(GatewayDenied, match="epoch_mismatch"):
        gateway.consume_permit(
            permit,  # type: ignore[arg-type]
            effect=EffectVerb.SET_BOOKMARK,
            intent_hash=intent.intent_hash(),
            actor_id="@actor",
            target_type="post",
            target_id="123",
            policy_binding=DEFAULT_EFFECT_POLICIES.require("bookmark").binding_hash(),
        )


def test_direct_epoch_bump_cannot_cross_permit_consumption(tmp_path: Path) -> None:
    """Consume and direct bump have one process-local linearization order."""
    epoch = _PausingEpoch()
    gateway = _gateway(tmp_path, epoch)
    intent = _bookmark_intent()
    grant, attempt = _claimed(intent, epoch)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    policy_binding = DEFAULT_EFFECT_POLICIES.require("bookmark").binding_hash()

    epoch.pause = True
    epoch.entered.clear()
    epoch.release.clear()
    result: list[object] = []

    def consume() -> None:
        try:
            gateway.consume_permit(
                permit,
                effect=EffectVerb.SET_BOOKMARK,
                intent_hash=intent.intent_hash(),
                actor_id="@actor",
                target_type="post",
                target_id="123",
                policy_binding=policy_binding,
            )
            result.append("consumed")
        except Exception as exc:  # pragma: no cover - asserted below
            result.append(exc)

    consumer = threading.Thread(target=consume)
    consumer.start()
    assert epoch.entered.wait(timeout=5)

    bump_done = threading.Event()

    def bump() -> None:
        epoch.bump()
        bump_done.set()

    bumper = threading.Thread(target=bump)
    bumper.start()
    assert not bump_done.is_set()

    epoch.release.set()
    consumer.join(timeout=10)
    bumper.join(timeout=10)
    assert not consumer.is_alive()
    assert not bumper.is_alive()
    assert result == ["consumed"]
    assert permit.consumed is True
    assert epoch.current == 1
