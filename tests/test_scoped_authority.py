"""M5 Layer-4 adversarial tests for least execution authority."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Optional

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
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
    AuthorizedEffect,
    PostPreparationAuthority,
    ReplyPreparationAuthority,
    ScopedAuthorityBroker,
    ScopedAuthorityDenied,
    SetBookmarkAuthority,
    SubmitContentAuthority,
)


class _FakeBroker:
    def __init__(self) -> None:
        self.events: list[tuple[Any, ...]] = []
        self.attachment_count = 0
        self.composer_text = ""
        self.attachments_ready = True
        self.bookmark_already_satisfied = False

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": self.attachments_ready})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": self.attachment_count})

    async def attach_media(self, path: str) -> ActionResult:
        self.events.append(("attach", path))
        self.attachment_count += 1
        return ok_result(data={"attached": True})

    async def close_composer(self) -> ActionResult:
        self.events.append(("close",))
        self.attachment_count = 0
        self.composer_text = ""
        return ok_result(data={"cleanup": "closed"})

    async def fill_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_post", text))
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def open_reply_on_target(self, url: str, post_id: str) -> ActionResult:
        self.events.append(("open_reply", url, post_id))
        return ok_result(data={"opened": True})

    async def fill_reply_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_reply", text))
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def open_quote_on_target(self, url: str, post_id: str) -> ActionResult:
        self.events.append(("open_quote", url, post_id))
        return ok_result(data={"opened": True})

    async def fill_quote_composer(self, text: str) -> ActionResult:
        self.events.append(("fill_quote", text))
        self.composer_text = text
        return ok_result(data={"filled": True})

    @staticmethod
    def _cross(gate):  # type: ignore[no-untyped-def]
        if gate is None:
            raise AssertionError("effect invoked without commit gate")
        return gate()

    async def click_bookmark(self, url: str, *, _commit_gate=None) -> ActionResult:  # type: ignore[no-untyped-def]
        if self.bookmark_already_satisfied:
            return ok_result(data={"bookmarked": True, "result": "already_satisfied"})
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

    async def click_submit(
        self,
        *,
        _commit_gate=None,  # type: ignore[no-untyped-def]
        _precommit_check=None,  # type: ignore[no-untyped-def]
        _expected_text: Optional[str] = None,
        _expected_attachments: Optional[int] = None,
    ) -> ActionResult:
        if _precommit_check is None or _expected_text is None or _expected_attachments is None:
            return soft_failure("missing scoped submit binding")
        denied = await _precommit_check()
        if denied is not None:
            return denied
        if self.composer_text != _expected_text:
            return soft_failure("composer text changed")
        if self.attachment_count != _expected_attachments or not self.attachments_ready:
            return soft_failure("composer media changed")
        if (denied := self._cross(_commit_gate)) is not None:
            return denied
        self.events.append(("submit", self.composer_text, self.attachment_count))
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
    return {
        "index": index,
        "source_path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def test_invalid_post_url_is_rejected_before_effect_handle(tmp_path: Path) -> None:
    intent = _intent("like", post_url="https://x.com/home")
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    with pytest.raises(ScopedAuthorityDenied, match="target_url_invalid"):
        scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    assert grant.state is GrantState.ACTIVE
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


def test_broadened_policy_is_rejected_before_effect_handle(tmp_path: Path) -> None:
    base = DEFAULT_EFFECT_POLICIES.require("bookmark")
    policies = EffectPolicyRegistry()
    policies.register(
        EffectPolicy.derive(
            action_type="bookmark",
            risk_tier=base.risk_tier,
            allowed_effects={EffectVerb.SET_BOOKMARK, EffectVerb.CLEAR_BOOKMARK},
            replay_semantics=ReplaySemantics.SAFE_STATE_SET,
        )
    )
    intent = _intent("bookmark")
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent, policies=policies)
    with pytest.raises(ScopedAuthorityDenied, match="effect_scope_not_exact"):
        scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    assert grant.state is GrantState.ACTIVE
    assert ledger.read_records() == []


async def test_effect_handle_does_not_mint_until_commit_gate(tmp_path: Path) -> None:
    intent = _intent("bookmark", post_url="")
    scoped, _, ledger, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(receipt, AuthorizedEffect)
    assert isinstance(receipt.authority, SetBookmarkAuthority)
    assert receipt.permit is None
    assert grant.state is GrantState.ACTIVE
    assert ledger.read_records() == []

    intent.payload["post_url"] = "https://x.com/other/status/999"
    result = await receipt.authority.apply()
    assert result.ok
    assert broker.events == [("bookmark", "https://x.com/i/status/123")]
    assert receipt.permit is not None and receipt.permit.consumed
    assert grant.state is GrantState.SPENT


async def test_already_satisfied_state_set_mints_nothing_and_spends_nothing(tmp_path: Path) -> None:
    intent = _intent("bookmark")
    broker = _FakeBroker()
    broker.bookmark_already_satisfied = True
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent, broker=broker)
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    result = await receipt.authority.apply()
    assert result.ok
    assert receipt.permit is None
    assert grant.state is GrantState.ACTIVE
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


def test_authority_surface_hides_outcome_lineage(tmp_path: Path) -> None:
    intent = _intent("bookmark")
    scoped, _, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    authority = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent).authority
    for forbidden in (
        "permit",
        "attempt",
        "write_broker",
        "browser",
        "click",
        "fill",
        "delete_post",
        "submit",
    ):
        assert not hasattr(authority, forbidden)


def test_content_cannot_scope_before_approved_preparation(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, ledger, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    with pytest.raises(ScopedAuthorityDenied, match="preparation_incomplete"):
        scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    assert grant.state is GrantState.ACTIVE
    assert ledger.read_records() == []


async def test_content_preparation_is_sealed_when_effect_handle_is_created(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, _, _, _, grant, attempt, _ = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(prep, PostPreparationAuthority)
    assert (await prep.fill_composer("approved text")).ok
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(receipt.authority, SubmitContentAuthority)
    assert receipt.permit is None
    with pytest.raises(ScopedAuthorityDenied, match="preparation_sealed"):
        await prep.fill_composer("approved text")
    with pytest.raises(ScopedAuthorityDenied, match="preparation_sealed"):
        scoped.prepare(grant=grant, attempt=attempt, intent=intent)


async def test_reply_preparation_enforces_context_then_text_then_media(tmp_path: Path) -> None:
    image = tmp_path / "image.bin"
    image.write_bytes(b"approved image")
    intent = _intent("reply", manifest_items=[_media_item(0, image)])
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert isinstance(prep, ReplyPreparationAuthority)
    with pytest.raises(ScopedAuthorityDenied, match="preparation_order"):
        await prep.fill_reply_composer("approved text")
    with pytest.raises(ScopedAuthorityDenied, match="preparation_order"):
        await prep.attach_media(str(image))
    assert (await prep.open_reply_on_target("https://x.com/u/status/123", "123")).ok
    assert (await prep.fill_reply_composer("approved text")).ok
    assert (await prep.attach_media(str(image))).ok
    assert broker.events == [
        ("open_reply", "https://x.com/u/status/123", "123"),
        ("fill_reply", "approved text"),
        ("attach", str(image)),
    ]


async def test_media_order_digest_and_duplicate_fill_are_bound(tmp_path: Path) -> None:
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    intent = _intent("post", manifest_items=[_media_item(0, first), _media_item(1, second)])
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert (await prep.fill_composer("approved text")).ok
    with pytest.raises(ScopedAuthorityDenied, match="preparation_order"):
        await prep.fill_composer("approved text")
    with pytest.raises(ScopedAuthorityDenied, match="media_order_mismatch"):
        await prep.attach_media(str(second))
    assert (await prep.attach_media(str(first))).ok
    second.write_bytes(b"changed after approval")
    with pytest.raises(ScopedAuthorityDenied, match="media_changed_after_approval"):
        await prep.attach_media(str(second))
    assert broker.events == [("fill_post", "approved text"), ("attach", str(first))]


async def test_revoke_blocks_preparation_but_cleanup_remains_available(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, _, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    grant.revoke()
    with pytest.raises(ScopedAuthorityDenied, match="grant_not_active"):
        await prep.fill_composer("approved text")
    assert (await prep.close_composer()).ok
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


async def test_submit_payload_drift_fails_before_permit_or_spend(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, _, ledger, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert (await prep.fill_composer("approved text")).ok
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    broker.composer_text = "different text"
    result = await receipt.authority.submit()  # type: ignore[union-attr]
    assert not result.ok
    assert receipt.permit is None
    assert grant.state is GrantState.ACTIVE
    assert attempt.state is AttemptState.PREPARING
    assert ledger.read_records() == []


async def test_submit_success_retains_exact_permit_for_gateway_outcome(tmp_path: Path) -> None:
    intent = _intent("post")
    scoped, gateway, ledger, _, _, grant, attempt, broker = _runtime(tmp_path, intent)
    prep = scoped.prepare(grant=grant, attempt=attempt, intent=intent)
    assert (await prep.fill_composer("approved text")).ok
    receipt = scoped.scope_effect(grant=grant, attempt=attempt, intent=intent)
    result = await receipt.authority.submit()  # type: ignore[union-attr]
    assert result.ok
    permit = receipt.permit
    assert permit is not None and permit.consumed
    assert grant.state is GrantState.SPENT
    assert broker.events[-1] == ("submit", "approved text", 0)
    gateway.record_effect_confirmed(permit, receipt.attempt, evidence={"test": "confirmed"})
    assert receipt.attempt.state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].state is EffectState.EFFECT_CONFIRMED
