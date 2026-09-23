"""Layer-4 integration guards across scoped authority and CommitGateway."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
    ReplaySemantics,
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
from webwire.safety.scoped_authority import (
    AuthorizedEffect,
    ScopedAuthorityBroker,
    ScopedAuthorityDenied,
    SetBookmarkAuthority,
)


class _EffectBroker:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    async def click_bookmark(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        assert _commit_gate is not None
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.events.append(("bookmark", url))
        return ok_result(data={"bookmarked": True})


def _bookmark_intent() -> WriteIntent:
    risk, comp = DEFAULT_REGISTRY.require("bookmark")
    return WriteIntent(
        action_type="bookmark",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=comp,
        actor_identity="@actor",
        payload={
            "post_url": "https://x.com/u/status/123",
            "post_id": "123",
        },
    )


def _registry() -> EffectPolicyRegistry:
    reg = EffectPolicyRegistry()
    reg.register(DEFAULT_EFFECT_POLICIES.require("bookmark"))
    return reg


def _runtime(
    tmp_path: Path,
    *,
    policies: EffectPolicyRegistry | None = None,
    clock=None,  # type: ignore[no-untyped-def]
):
    intent = _bookmark_intent()
    reg = policies or _registry()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    ledger = EffectLedger(cfg)
    kill = KillSwitch(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
        policies=reg,
        clock=clock if clock is not None else __import__("time").monotonic,
        permit_ttl_seconds=1.0 if clock is not None else 60.0,
    )
    broker = _EffectBroker()
    scoped = ScopedAuthorityBroker(broker, gateway, policies=reg)
    policy = reg.require("bookmark")
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
    return intent, reg, ledger, kill, epoch, gateway, scoped, broker, grant, attempt


def _bookmark_receipt(tmp_path: Path):  # type: ignore[no-untyped-def]
    runtime = _runtime(tmp_path)
    intent, _, _, _, _, _, scoped, _, grant, attempt = runtime
    receipt = scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(receipt, AuthorizedEffect)
    assert isinstance(receipt.authority, SetBookmarkAuthority)
    return runtime, receipt


def test_scoped_and_gateway_must_share_exact_policy_registry(tmp_path: Path) -> None:
    intent, reg, _, _, _, gateway, _, broker, _, _ = _runtime(tmp_path)
    other = EffectPolicyRegistry()
    other.register(reg.require(intent.action_type))

    with pytest.raises(ScopedAuthorityDenied, match="policy_registry_mismatch"):
        ScopedAuthorityBroker(broker, gateway, policies=other)


async def test_kill_trip_then_reset_still_blocks_minted_authority(tmp_path: Path) -> None:
    runtime, receipt = _bookmark_receipt(tmp_path)
    _, _, _, kill, _, _, _, broker, _, _ = runtime

    kill.trip()
    kill.reset()
    result = await receipt.authority.apply()

    assert not result.ok
    assert broker.events == []
    assert receipt.permit.consumed is False


async def test_policy_drift_after_mint_blocks_browser_mutation(tmp_path: Path) -> None:
    runtime, receipt = _bookmark_receipt(tmp_path)
    _, reg, _, _, _, _, _, broker, _, _ = runtime
    base = reg.require("bookmark")
    reg.register(
        EffectPolicy.derive(
            action_type="bookmark",
            risk_tier=base.risk_tier,
            allowed_effects={EffectVerb.SET_BOOKMARK},
            replay_semantics=ReplaySemantics.UNKNOWN,
        )
    )

    result = await receipt.authority.apply()

    assert not result.ok
    assert broker.events == []
    assert receipt.permit.consumed is False


async def test_expired_permit_blocks_browser_mutation_and_closes_attempt(tmp_path: Path) -> None:
    now = [0.0]
    runtime = _runtime(tmp_path, clock=lambda: now[0])
    intent, _, _, _, _, _, scoped, broker, grant, attempt = runtime
    receipt = scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(receipt.authority, SetBookmarkAuthority)

    now[0] = 2.0
    result = await receipt.authority.apply()

    assert not result.ok
    assert broker.events == []
    assert attempt.state is AttemptState.NO_EFFECT
    assert receipt.permit.consumed is False


async def test_consumed_permit_cannot_mutate_twice(tmp_path: Path) -> None:
    runtime, receipt = _bookmark_receipt(tmp_path)
    _, _, _, _, _, _, _, broker, _, _ = runtime

    first = await receipt.authority.apply()
    second = await receipt.authority.apply()

    assert first.ok
    assert not second.ok
    assert broker.events == [("bookmark", "https://x.com/u/status/123")]
    assert receipt.permit.consumed is True


def test_policy_broadening_is_denied_before_gateway_mint(tmp_path: Path) -> None:
    base = DEFAULT_EFFECT_POLICIES.require("bookmark")
    reg = EffectPolicyRegistry()
    reg.register(
        EffectPolicy.derive(
            action_type="bookmark",
            risk_tier=base.risk_tier,
            allowed_effects={EffectVerb.SET_BOOKMARK, EffectVerb.CLEAR_BOOKMARK},
            replay_semantics=ReplaySemantics.SAFE_STATE_SET,
        )
    )
    runtime = _runtime(tmp_path, policies=reg)
    intent, _, ledger, _, _, _, scoped, _, grant, attempt = runtime

    with pytest.raises(ScopedAuthorityDenied, match="effect_scope_not_exact"):
        scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert ledger.read_records() == []
    assert attempt.state is AttemptState.PREPARING
