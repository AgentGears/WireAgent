"""Single-use object semantics for Layer-4 effect authorities."""

from __future__ import annotations

import asyncio
from pathlib import Path

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker


class _BlockingBookmarkBroker:
    def __init__(self) -> None:
        self.calls = 0
        self.crossed = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def click_bookmark(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        self.calls += 1
        self.started.set()
        if self.block:
            await self.release.wait()
        if _commit_gate is None:
            raise AssertionError("missing commit gate")
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.crossed += 1
        return ok_result(data={"bookmarked": True, "url": url})


def _runtime(tmp_path: Path):  # type: ignore[no-untyped-def]
    risk, comp = DEFAULT_REGISTRY.require("bookmark")
    intent = WriteIntent(
        action_type="bookmark",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=comp,
        actor_identity="@actor",
        semantic_variant="approved",
        payload={"post_id": "123", "post_url": "https://x.com/u/status/123"},
    )
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    ledger = EffectLedger(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=epoch,
        permit_ttl_seconds=60.0,
    )
    policy = DEFAULT_EFFECT_POLICIES.require("bookmark")
    grant = ApprovalGrantStore().mint(
        intent_hash=intent.intent_hash(),
        actor_id="@actor",
        action_type="bookmark",
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
    broker = _BlockingBookmarkBroker()
    scoped = ScopedAuthorityBroker(broker, gateway)
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    return receipt, broker


async def test_post_crossing_reuse_is_rejected_before_second_broker_call(tmp_path: Path) -> None:
    receipt, broker = _runtime(tmp_path)

    first = await receipt.authority.apply()
    assert first.ok
    assert broker.calls == 1
    assert broker.crossed == 1
    assert receipt.permit is not None and receipt.permit.consumed

    second = await receipt.authority.apply()
    assert not second.ok
    assert broker.calls == 1
    assert broker.crossed == 1


async def test_concurrent_second_invocation_is_rejected_before_broker_call(tmp_path: Path) -> None:
    receipt, broker = _runtime(tmp_path)
    broker.block = True

    first_task = asyncio.create_task(receipt.authority.apply())
    await broker.started.wait()

    second = await receipt.authority.apply()
    assert not second.ok
    assert broker.calls == 1

    broker.release.set()
    first = await first_task
    assert first.ok
    assert broker.calls == 1
    assert broker.crossed == 1
