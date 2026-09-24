"""Concurrency regressions for M5 Layer-4 scoped authority."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

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


class _BlockingBroker:
    def __init__(self) -> None:
        self.fill_started = asyncio.Event()
        self.fill_release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.submit_started = asyncio.Event()
        self.submit_release = asyncio.Event()
        self.fill_calls = 0
        self.composer_text = ""
        self.events: list[str] = []

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, path: str) -> ActionResult:
        raise AssertionError(f"unexpected attachment: {path}")

    async def close_composer(self) -> ActionResult:
        self.events.append("close_started")
        self.close_started.set()
        await self.close_release.wait()
        self.composer_text = ""
        self.events.append("close_finished")
        return ok_result(data={"cleanup": "closed"})

    async def fill_composer(self, text: str) -> ActionResult:
        self.fill_calls += 1
        self.events.append(f"fill_started:{self.fill_calls}")
        self.fill_started.set()
        await self.fill_release.wait()
        self.composer_text = text
        self.events.append(f"fill_finished:{self.fill_calls}")
        return ok_result(data={"filled": True})

    async def click_submit(
        self,
        *,
        _commit_gate=None,  # type: ignore[no-untyped-def]
        _precommit_check=None,  # type: ignore[no-untyped-def]
        _expected_text: Optional[str] = None,
        _expected_attachments: Optional[int] = None,
    ) -> ActionResult:
        assert _commit_gate is not None
        assert _precommit_check is not None
        assert _expected_text == "approved text"
        assert _expected_attachments == 0
        denied = await _precommit_check()
        if denied is not None:
            return denied
        self.events.append("submit_started")
        self.submit_started.set()
        await self.submit_release.wait()
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.events.append("submit_clicked")
        return ok_result(data={"submitted": True})


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
    broker = _BlockingBroker()
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


async def test_second_stage_is_rejected_while_first_owns_latch(tmp_path: Path) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    first = asyncio.create_task(prep.fill_composer("approved text"))
    await broker.fill_started.wait()
    with pytest.raises(ScopedAuthorityDenied, match="preparation_in_flight"):
        await prep.fill_composer("approved text")
    assert broker.fill_calls == 1
    broker.fill_release.set()
    assert (await first).ok
    scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)


async def test_cleanup_invalidates_stage_but_cannot_release_its_owner_early(
    tmp_path: Path,
) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)

    first = asyncio.create_task(prep.fill_composer("approved text"))
    await broker.fill_started.wait()
    cleanup_task = asyncio.create_task(prep.close_composer())
    await broker.close_started.wait()

    broker.close_release.set()
    assert (await cleanup_task).ok

    # Cleanup invalidated A's logical result, but A still owns the async-stage
    # token until A actually returns. A newer B cannot start under it.
    with pytest.raises(ScopedAuthorityDenied, match="preparation_in_flight"):
        await prep.fill_composer("approved text")

    broker.fill_release.set()
    assert (await first).ok

    # Stale A did not restore readiness; a clean new stage is required.
    with pytest.raises(ScopedAuthorityDenied, match="preparation_incomplete"):
        scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)

    broker.fill_started = asyncio.Event()
    broker.fill_release = asyncio.Event()
    second = asyncio.create_task(prep.fill_composer("approved text"))
    await broker.fill_started.wait()
    broker.fill_release.set()
    assert (await second).ok
    scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)


async def test_cleanup_cannot_interleave_with_submit_effect_invocation(tmp_path: Path) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    broker.fill_release.set()
    assert (await prep.fill_composer("approved text")).ok
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)

    submit = asyncio.create_task(receipt.authority.submit())  # type: ignore[union-attr]
    await broker.submit_started.wait()

    with pytest.raises(ScopedAuthorityDenied, match="effect_in_flight"):
        await prep.close_composer()

    broker.submit_release.set()
    assert (await submit).ok
    assert "submit_clicked" in broker.events


async def test_submit_cannot_start_while_cleanup_is_active(tmp_path: Path) -> None:
    intent, scoped, broker, grant, attempt = _runtime(tmp_path)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    broker.fill_release.set()
    assert (await prep.fill_composer("approved text")).ok
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)

    cleanup = asyncio.create_task(prep.close_composer())
    await broker.close_started.wait()
    with pytest.raises(ScopedAuthorityDenied, match="preparation_cleanup_in_flight"):
        await receipt.authority.submit()  # type: ignore[union-attr]

    broker.close_release.set()
    assert (await cleanup).ok
    assert receipt.permit is None
