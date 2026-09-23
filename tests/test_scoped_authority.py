"""M5 Layer-4 adversarial tests for least execution authority."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway, EffectPermit
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
    PreparationVerb,
    ReplaySemantics,
)
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
from webwire.safety.scoped_authority import (
    PostPreparationAuthority,
    ReplyPreparationAuthority,
    ScopedAuthorityBroker,
    ScopedAuthorityDenied,
    SetBookmarkAuthority,
    SubmitContentAuthority,
)


class _FakeBroker:
    """Minimal broker whose effect methods cross the supplied private gate."""

    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.attachment_count = 0

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": ""})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": self.attachment_count})

    async def attach_media(self, path: str) -> ActionResult:
        self.events.append(("attach", path))
        self.attachment_count += 1
        return ok_result(data={"attached": True})

    async def close_composer(self) -> ActionResult:
        self.events.append(("close",))
        self.attachment_count = 0
        return ok_result(data={"cleanup": "closed"})

    async def fill_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_post", text))
        return ok_result(data={"filled": True})

    async def open_reply_on_target(self, url: str, post_id: str) -> ActionResult:
        self.events.append(("open_reply", url, post_id))
        return ok_result(data={"opened": True})

    async def fill_reply_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_reply", text))
        return ok_result(data={"filled": True})

    async def open_quote_on_target(self, url: str, post_id: str) -> ActionResult:
        self.events.append(("open_quote", url, post_id))
        return ok_result(data={"opened": True})

    async def fill_quote_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_quote", text))
        return ok_result(data={"filled": True})

    @staticmethod
    def _cross(gate):  # type: ignore[no-untyped-def]
        if gate is None:
            raise AssertionError("effect invoked without commit gate")
        return gate()

    async def click_bookmark(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("bookmark", url))
        return ok_result(data={"bookmarked": True})

    async def click_remove_bookmark(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("remove_bookmark", url))
        return ok_result(data={"bookmarked": False})

    async def click_like(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("like", url))
        return ok_result(data={"liked": True})

    async def click_unlike(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("unlike", url))
        return ok_result(data={"liked": False})

    async def click_submit(self, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("submit",))
        return ok_result(data={"submitted": True})

    async def delete_post(self, url: str, post_id: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("delete", url, post_id))
        return ok_result(data={"deleted": True})


def _intent(
    action: str,
    *,
    post_id: str = "123",
    post_url: str | None = None,
    text: str = "approved text",
    manifest_items: list[dict[str, Any]] | None = None,
) -> WriteIntent:
    risk, comp = DEFAULT_REGISTRY.require(action)
    if action == "post":
        return WriteIntent(
            action_type="post",
            target_type="none",
            target_id="none",
            risk_meta=risk,
            compensation=comp,
            actor_identity="@actor",
            semantic_variant="approved",
            payload={
                "normalized_text": text,
                "char_count": len(text),
                "manifest_items": manifest_items or [],
            },
        )

    payload: dict[str, Any] = {
        "post_url": post_url if post_url is not None else f"https://x.com/u/status/{post_id}",
    }
    if action in {"reply", "quote", "delete_post"}:
        payload["target_post_id"] = post_id
    else:
        payload["post_id"] = post_id
    if action in {"reply", "quote"}:
        payload["normalized_text"] = text
        payload["char_count"] = len(text)
        payload["manifest_items"] = manifest_items or []

    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id=post_id,
        risk_meta=risk,
        compensation=comp,
        actor_identity="@actor",
        semantic_variant="approved",
        payload=payload,
    )


def _claim(
    intent: WriteIntent,
    epoch: AuthorizationEpoch,
    *,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
) -> tuple[ApprovalGrant, EffectAttempt]:
    policy = policies.require(intent.action_type)
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


def _runtime(
    tmp_path: Path,
    intent: WriteIntent,
    *,
    broker: _FakeBroker | None = None,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
):  # type: ignore[no-untyped-def]
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    ledger = EffectLedger(cfg)
    kill = KillSwitch(cfg)
    epoch = AuthorizationEpoch()
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=epoch,
        policies=policies,
        permit_ttl_seconds=60.0,
    )
    real_broker = broker or _FakeBroker()
    scoped = ScopedAuthorityBroker(real_broker, gateway, policies=policies)
    grant, attempt = _claim(intent, epoch, policies=policies)
    return scoped, gateway, ledger, kill, epoch, grant, attempt, real_broker


def _media_item(index: int, path: Path) -> dict[str, Any]:
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"index": index, "source_path": str(path), "sha256": digest}


def test_invalid_post_url_is_rejected_before_reservation_or_spend(tmp_path: Path) -> None:
    intent = _intent("like", post_url="https://x.com/home")
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent)

    with pytest.raises(ScopedAuthorityDenied, match="target_url_invalid"):
        scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert grant.state is GrantState.ACTIVE
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


def test_mismatched_status_url_is_rejected_before_reservation_or_spend(tmp_path: Path) -> None:
    intent = _intent("delete_post", post_id="123", post_url="https://x.com/u/status/999")
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent)

    with pytest.raises(ScopedAuthorityDenied, match="target_mismatch"):
        scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert grant.state is GrantState.ACTIVE
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


async def test_missing_status_url_is_canonicalized_and_frozen(tmp_path: Path) -> None:
    intent = _intent("bookmark", post_url="")
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    authority = scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(authority, SetBookmarkAuthority)

    # Caller-owned mutable intent no longer controls the authority.
    intent.payload["post_url"] = "https://x.com/other/status/999"
    intent.payload["post_id"] = "999"
    result = await authority.apply()

    assert result.ok
    assert broker.events == [("bookmark", "https://x.com/i/status/123")]
    assert authority.consumed is True


def test_authority_surfaces_are_direction_specific_and_do_not_expose_broker(tmp_path: Path) -> None:
    intent = _intent("bookmark")
    scoped, _, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    authority = scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)

    assert hasattr(authority, "apply")
    for forbidden in (
        "click_bookmark",
        "click_remove_bookmark",
        "click_like",
        "delete_post",
        "submit",
        "fill_composer",
        "write_broker",
        "browser",
        "click",
        "fill",
    ):
        assert not hasattr(authority, forbidden)


def test_submit_and_preparation_surfaces_are_separate(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(prep, PostPreparationAuthority)
    assert hasattr(prep, "fill_composer")
    assert not hasattr(prep, "submit")
    assert not hasattr(prep, "delete")
    assert not hasattr(prep, "apply")

    submit = scoped.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(submit, SubmitContentAuthority)
    assert hasattr(submit, "submit")
    assert not hasattr(submit, "fill_composer")
    assert not hasattr(submit, "attach_media")
    assert not hasattr(submit, "delete")


async def test_reply_preparation_enforces_context_then_text_then_media(tmp_path: Path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"approved image")
    item = _media_item(0, image)
    intent = _intent("reply", manifest_items=[item])
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(prep, ReplyPreparationAuthority)

    with pytest.raises(ScopedAuthorityDenied, match="preparation_order"):
        await prep.fill_reply_composer("approved text")
    with pytest.raises(ScopedAuthorityDenied, match="preparation_order"):
        await prep.attach_media(str(image))

    await prep.open_reply_on_target("https://x.com/u/status/123", "123")
    await prep.fill_reply_composer("approved text")
    await prep.attach_media(str(image))
    assert broker.events == [
        ("open_reply", "https://x.com/u/status/123", "123"),
        ("fill_reply", "approved text"),
        ("attach", str(image)),
    ]


async def test_media_path_order_and_digest_are_bound(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    intent = _intent("post", manifest_items=[_media_item(0, first), _media_item(1, second)])
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    await prep.fill_composer("approved text")

    with pytest.raises(ScopedAuthorityDenied, match="media_order_mismatch"):
        await prep.attach_media(str(second))
    await prep.attach_media(str(first))

    second.write_bytes(b"changed after approval")
    with pytest.raises(ScopedAuthorityDenied, match="media_changed_after_approval"):
        await prep.attach_media(str(second))

    assert broker.events == [
        ("fill_post", "approved text"),
        ("attach", str(first)),
    ]


async def test_preparation_fails_after_revoke_but_cleanup_remains_available(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    grant.revoke()

    with pytest.raises(ScopedAuthorityDenied, match="grant_not_active"):
        await prep.fill_composer("approved text")
    cleanup = await prep.close_composer()
    assert cleanup.ok
    assert broker.events == [("close",)]


def test_policy_binding_drift_invalidates_existing_preparation(tmp_path: Path) -> None:
    base = DEFAULT_EFFECT_POLICIES.require("post")
    policies = EffectPolicyRegistry()
    policies.register(base)
    intent = _intent("post")
    scoped, _, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent, policies=policies)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)

    policies.register(
        EffectPolicy.derive(
            action_type="post",
            risk_tier=base.risk_tier,
            allowed_effects={EffectVerb.SUBMIT_CONTENT},
            replay_semantics=ReplaySemantics.NON_IDEMPOTENT_CREATE,
            preparation_effects={PreparationVerb.FILL_COMPOSER},
        )
    )
    with pytest.raises(ScopedAuthorityDenied, match="policy_mismatch"):
        prep._require_live(PreparationVerb.FILL_COMPOSER)  # type: ignore[attr-defined]


def test_broadened_permit_is_rejected_before_browser_authority(tmp_path: Path) -> None:
    intent = _intent("bookmark")
    scoped, gateway, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    permit = gateway.authorize_commit(grant=grant, attempt=attempt, intent=intent)
    broader: EffectPermit = replace(
        permit,
        allowed_effects=frozenset({EffectVerb.SET_BOOKMARK, EffectVerb.CLEAR_BOOKMARK}),
    )

    with pytest.raises(ScopedAuthorityDenied, match="effect_scope_not_exact"):
        scoped.authorize(permit=broader, attempt=attempt, intent=intent)
