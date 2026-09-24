"""Concurrency regressions for M5 Layer-4 preparation authority."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import ApprovalGrantStore, AuthorizationEpoch, EffectAttempt
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied


class _BlockingPreparationBroker:
    def __init__(self) -> None:
        self.fill_started = asyncio.Event()
        self.fill_release = asyncio.Event()
        self.fill_calls = 0
        self.composer_text = ""

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, path: str) -> ActionResult:
        raise AssertionError(f"unexpected attachment: {path}")

    async def close_composer(self) -> ActionResult:
        self.composer_text = ""
        return ok_result(data={"cleanup": "closed"})

    async def fill_composer(self, text: str) -> ActionResult:
        self.fill_calls += 1
        self.fill_started.set()
        await self.fill_release.wait()
        self.composer_text = text
        return ok_result(data={"filled": True})


def _post_intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="none",
        target_id="none",
        risk_meta=risk,
        compensation=compensation,
        actor_identity="@actor",
        semantic_variant="approved",
        payload={
            "normalized_text": "approved text",
            "char_count": len("approved text"),
            "manifest_items": [],
        },
    )


def _runtime(tmp_path: Path):  # type: ignore[no-untyped-def]
    intent = _post_intent()
    config = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=EffectLedger(config),
        kill_switch=KillSwitch(config),
        authorization_epoch=epoch,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    broker = _BlockingPreparationBroker()
    scoped = ScopedAuthorityBroker(broker, gateway)
    policy = DEFAULT_EFFECT_POLICIES.require("post")
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
    return intent, scoped, broker, grant, attempt


async def test_second_staging_mutation_is_rejected_while_first_is_in_flight(
    tmp_path: Path,
) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)

    first = asyncio.create_task(prep.fill_composer("approved text"))
    await broker.fill_started.wait()

    with pytest.raises(ScopedAuthorityDenied, match="preparation_in_flight"):
        await prep.fill_composer("approved text")

    assert broker.fill_calls == 1
    broker.fill_release.set()
    assert (await first).ok

    # The one successful operation can make the content handle ready.
    scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)


async def test_cleanup_invalidates_in_flight_success_before_readiness(
    tmp_path: Path,
) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)

    first = asyncio.create_task(prep.fill_composer("approved text"))
    await broker.fill_started.wait()

    cleanup = await prep.close_composer()
    assert cleanup.ok

    # Cleanup does not let a second mutation start while the first broker call
    # is still outstanding.
    with pytest.raises(ScopedAuthorityDenied, match="preparation_in_flight"):
        await prep.fill_composer("approved text")

    broker.fill_release.set()
    assert (await first).ok

    # The stale completion is not allowed to resurrect readiness after cleanup.
    with pytest.raises(ScopedAuthorityDenied, match="preparation_incomplete"):
        scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
