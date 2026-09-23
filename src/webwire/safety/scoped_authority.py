"""M5 layer 4 — intent-bound preparation and exact effect authorities.

This module is the process-local least-authority adapter between Layer 3's
CommitGateway/EffectPermit and the concrete M5 write broker. It does not wire
the live WriteKernel; capability migration remains Layer 5.

Scoped objects retain only exact bound operations, never the concrete broker
object itself. Effect authorities also do not expose outcome-recording APIs:
verification and durable terminal truth remain CommitGateway orchestration owned
by Layer 5.

Threat model: same-process engineering boundary against accidental overreach,
not a hostile-Python sandbox. Untrusted code still requires process/OS isolation
and no raw browser handle.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, soft_failure
from webwire.safety.attachment import file_sha256
from webwire.safety.commit_gateway import CommitGateway, EffectPermit, GatewayDenied
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicyRegistry,
    EffectVerb,
    PreparationVerb,
)
from webwire.safety.execution_models import (
    ApprovalGrant,
    AttemptState,
    EffectAttempt,
    GrantClaimDenied,
    GrantState,
)
from webwire.safety.models import WriteIntent

__all__ = [
    "ScopedAuthorityBroker",
    "ScopedAuthorityDenied",
    "PostPreparationAuthority",
    "ReplyPreparationAuthority",
    "QuotePreparationAuthority",
    "SetBookmarkAuthority",
    "ClearBookmarkAuthority",
    "SetLikeAuthority",
    "ClearLikeAuthority",
    "DeletePostAuthority",
    "SubmitContentAuthority",
]

_AsyncNoArg = Callable[[], Awaitable[ActionResult]]
_AsyncOneStr = Callable[[str], Awaitable[ActionResult]]
_AsyncTwoStr = Callable[[str, str], Awaitable[ActionResult]]
_CommitGate = Callable[[], Optional[ActionResult]]
_EffectInvocation = Callable[[_CommitGate], Awaitable[ActionResult]]

_POST_TARGET_ACTIONS = frozenset(
    {
        "bookmark",
        "remove_bookmark",
        "like",
        "unlike",
        "reply",
        "quote",
        "delete_post",
    }
)
_STATUS_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_STATUS_PATH_RE = re.compile(r"/status/(\d+)(?:/|$)")


class ScopedAuthorityDenied(RuntimeError):
    """A scoped-authority construction or use request violated its binding."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"scoped authority denied: {reason}" + (f" — {detail}" if detail else "")
        )


def _bind_status_url(raw_url: str, target_post_id: str) -> str:
    """Return a target-safe X status URL or reject the binding.

    A permit target id is not enough if the browser can be navigated to an
    unrelated page and a page-global state-set selector can act on another post.
    Supplied URLs therefore must themselves identify the approved status. When a
    caller omits the URL, derive a canonical X status route from the approved id.
    """
    if not raw_url:
        return f"https://x.com/i/status/{target_post_id}"

    parsed = urlparse(raw_url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme.lower() != "https" or host not in _STATUS_HOSTS:
        raise ScopedAuthorityDenied(
            "target_url_invalid",
            "post target URL must be an HTTPS x.com/twitter.com status URL",
        )
    match = _STATUS_PATH_RE.search(parsed.path)
    if match is None:
        raise ScopedAuthorityDenied(
            "target_url_invalid",
            "post target URL must contain /status/<approved-id>",
        )
    if match.group(1) != target_post_id:
        raise ScopedAuthorityDenied(
            "target_mismatch",
            f"URL target {match.group(1)!r} != approved target {target_post_id!r}",
        )
    return raw_url


@dataclass(frozen=True)
class _MediaBinding:
    index: int
    source_path: str
    sha256: str


@dataclass(frozen=True)
class _IntentBinding:
    """Private immutable values extracted from one deep-copied WriteIntent."""

    action_type: str
    target_type: str
    target_id: str
    actor_id: str
    intent_hash: str
    policy_binding: str
    post_url: str
    target_post_id: str
    normalized_text: str
    media: tuple[_MediaBinding, ...]

    @classmethod
    def capture(cls, intent: WriteIntent, *, policy_binding: str) -> "_IntentBinding":
        frozen = deepcopy(intent)
        actor_id = frozen.actor_identity or ""
        if not actor_id:
            raise ScopedAuthorityDenied("actor_missing")

        payload = frozen.payload
        if not isinstance(payload, dict):
            raise ScopedAuthorityDenied("payload_invalid", "intent payload must be a dict")

        target_id = frozen.target_id
        target_type = frozen.target_type
        if not isinstance(target_type, str) or not target_type:
            raise ScopedAuthorityDenied("target_missing", "target_type must be non-empty")
        if not isinstance(target_id, str) or not target_id:
            raise ScopedAuthorityDenied("target_missing", "target_id must be non-empty")

        if (
            frozen.action_type in _POST_TARGET_ACTIONS
            and (target_type != "post" or not target_id.isdigit())
        ):
            raise ScopedAuthorityDenied(
                "target_invalid",
                f"{frozen.action_type} requires a numeric post target",
            )

        post_url_raw = payload.get("post_url", "")
        if post_url_raw is None:
            post_url_raw = ""
        if not isinstance(post_url_raw, str):
            raise ScopedAuthorityDenied("payload_invalid", "post_url must be a string")

        payload_target = payload.get("target_post_id", payload.get("post_id", ""))
        if payload_target is None:
            payload_target = ""
        if not isinstance(payload_target, str):
            raise ScopedAuthorityDenied(
                "payload_invalid", "post_id/target_post_id must be a string"
            )

        target_post_id = payload_target or (target_id if target_type == "post" else "")
        if target_post_id and target_id not in {"none", target_post_id}:
            raise ScopedAuthorityDenied(
                "target_mismatch",
                f"payload target {target_post_id!r} != intent target {target_id!r}",
            )
        if frozen.action_type in _POST_TARGET_ACTIONS and target_post_id != target_id:
            raise ScopedAuthorityDenied("target_mismatch")

        post_url = post_url_raw
        if frozen.action_type in _POST_TARGET_ACTIONS:
            post_url = _bind_status_url(post_url_raw, target_post_id)
        elif not post_url and target_post_id:
            post_url = f"https://x.com/i/status/{target_post_id}"

        normalized_text_raw = payload.get("normalized_text", "")
        if normalized_text_raw is None:
            normalized_text_raw = ""
        if not isinstance(normalized_text_raw, str):
            raise ScopedAuthorityDenied(
                "payload_invalid", "normalized_text must be a string"
            )

        media_raw = payload.get("manifest_items", [])
        if media_raw is None:
            media_raw = []
        if not isinstance(media_raw, list):
            raise ScopedAuthorityDenied("payload_invalid", "manifest_items must be a list")
        media: list[_MediaBinding] = []
        for expected_index, item in enumerate(media_raw):
            if not isinstance(item, dict):
                raise ScopedAuthorityDenied("payload_invalid", "manifest item must be a dict")
            index = item.get("index")
            source_path = item.get("source_path")
            digest = item.get("sha256")
            if index != expected_index:
                raise ScopedAuthorityDenied(
                    "media_order_mismatch",
                    f"manifest index {index!r} != expected {expected_index}",
                )
            if not isinstance(source_path, str) or not source_path:
                raise ScopedAuthorityDenied(
                    "payload_invalid", f"manifest item {expected_index} has invalid path"
                )
            if not isinstance(digest, str) or not digest:
                raise ScopedAuthorityDenied(
                    "payload_invalid", f"manifest item {expected_index} has invalid digest"
                )
            media.append(
                _MediaBinding(
                    index=expected_index,
                    source_path=source_path,
                    sha256=digest,
                )
            )

        return cls(
            action_type=frozen.action_type,
            target_type=target_type,
            target_id=target_id,
            actor_id=actor_id,
            intent_hash=frozen.intent_hash(),
            policy_binding=policy_binding,
            post_url=post_url,
            target_post_id=target_post_id,
            normalized_text=normalized_text_raw,
            media=tuple(media),
        )


_EFFECT_BY_ACTION: dict[str, EffectVerb] = {
    "bookmark": EffectVerb.SET_BOOKMARK,
    "remove_bookmark": EffectVerb.CLEAR_BOOKMARK,
    "like": EffectVerb.SET_LIKE,
    "unlike": EffectVerb.CLEAR_LIKE,
    "post": EffectVerb.SUBMIT_CONTENT,
    "reply": EffectVerb.SUBMIT_CONTENT,
    "quote": EffectVerb.SUBMIT_CONTENT,
    "delete_post": EffectVerb.DELETE_POST,
}


class _PreparationBase:
    __slots__ = (
        "__grant",
        "__attempt",
        "__binding",
        "__policies",
        "__gateway",
        "__next_media_index",
        "__composer_opened",
        "__text_filled",
        "__read_composer",
        "__verify_attachment",
        "__count_attachments",
        "__attach_media",
        "__close_composer",
    )

    def __init__(
        self,
        *,
        write_broker: Any,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        binding: _IntentBinding,
        policies: EffectPolicyRegistry,
        gateway: CommitGateway,
    ) -> None:
        self.__grant = grant
        self.__attempt = attempt
        self.__binding = binding
        self.__policies = policies
        self.__gateway = gateway
        self.__next_media_index = 0
        self.__composer_opened = False
        self.__text_filled = False
        self.__read_composer: _AsyncNoArg = write_broker.read_composer_text
        self.__verify_attachment: _AsyncNoArg = write_broker.verify_attachment_ready
        self.__count_attachments: _AsyncNoArg = write_broker.count_attachments
        self.__attach_media: _AsyncOneStr = write_broker.attach_media
        self.__close_composer: _AsyncNoArg = write_broker.close_composer

    @property
    def action_type(self) -> str:
        return self.__binding.action_type

    @property
    def intent_hash(self) -> str:
        return self.__binding.intent_hash

    def _expected_text(self) -> str:
        return self.__binding.normalized_text

    def _expected_post_url(self) -> str:
        return self.__binding.post_url

    def _expected_target_post_id(self) -> str:
        return self.__binding.target_post_id

    def _mark_composer_opened(self) -> None:
        self.__composer_opened = True

    def _mark_text_filled(self) -> None:
        self.__text_filled = True

    def _require_composer_opened(self) -> None:
        if not self.__composer_opened:
            raise ScopedAuthorityDenied(
                "preparation_order", "approved composer context has not been opened"
            )

    def _require_text_filled(self) -> None:
        if not self.__text_filled:
            raise ScopedAuthorityDenied(
                "preparation_order", "approved composer text has not been staged"
            )

    def _reset_preparation(self) -> None:
        self.__composer_opened = False
        self.__text_filled = False
        self.__next_media_index = 0

    def _require_live(self, verb: PreparationVerb) -> None:
        grant = self.__grant
        attempt = self.__attempt
        binding = self.__binding

        if attempt.grant_id != grant.grant_id:
            raise ScopedAuthorityDenied("grant_mismatch")
        if grant.claimed_by != attempt.attempt_id:
            raise ScopedAuthorityDenied("claim_not_held")
        if attempt.state is not AttemptState.PREPARING:
            raise ScopedAuthorityDenied("attempt_not_preparing", attempt.state.value)
        if grant.state is not GrantState.ACTIVE:
            raise ScopedAuthorityDenied("grant_not_active", grant.state.value)
        if grant.action_type != binding.action_type:
            raise ScopedAuthorityDenied("action_mismatch")
        if grant.target_type != binding.target_type or grant.target_id != binding.target_id:
            raise ScopedAuthorityDenied("target_mismatch")

        try:
            policy = self.__policies.require(binding.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        policy.validate()
        current_binding = policy.binding_hash()
        if current_binding != binding.policy_binding:
            raise ScopedAuthorityDenied("policy_mismatch")
        if verb not in policy.preparation_effects:
            raise ScopedAuthorityDenied("preparation_not_allowed", verb.value)

        try:
            grant.validate_live(
                intent_hash=binding.intent_hash,
                actor_id=binding.actor_id,
                policy_binding=current_binding,
                authorization_epoch=self.__gateway.authorization_epoch,
            )
        except GrantClaimDenied as exc:
            raise ScopedAuthorityDenied(exc.reason, str(exc)) from exc

    async def read_composer_text(self) -> ActionResult:
        return await self.__read_composer()

    async def verify_attachment_ready(self) -> ActionResult:
        return await self.__verify_attachment()

    async def count_attachments(self) -> ActionResult:
        return await self.__count_attachments()

    async def attach_media(self, image_path: str) -> ActionResult:
        self._require_live(PreparationVerb.ATTACH_MEDIA)
        self._require_text_filled()
        if self.__next_media_index >= len(self.__binding.media):
            raise ScopedAuthorityDenied("media_not_approved", image_path)
        expected = self.__binding.media[self.__next_media_index]
        if image_path != expected.source_path:
            raise ScopedAuthorityDenied(
                "media_order_mismatch",
                f"expected {expected.source_path!r}, got {image_path!r}",
            )
        try:
            digest = file_sha256(Path(expected.source_path))
        except OSError as exc:
            raise ScopedAuthorityDenied("media_unreadable", str(exc)) from exc
        if digest != expected.sha256:
            raise ScopedAuthorityDenied(
                "media_changed_after_approval",
                f"media index {expected.index} digest changed",
            )
        self._require_live(PreparationVerb.ATTACH_MEDIA)
        result = await self.__attach_media(expected.source_path)
        if result.ok:
            self.__next_media_index += 1
        return result

    async def close_composer(self) -> ActionResult:
        """Reducing cleanup remains available even when approval was revoked."""
        result = await self.__close_composer()
        if result.ok:
            self._reset_preparation()
        return result


class PostPreparationAuthority(_PreparationBase):
    __slots__ = ("__fill_composer",)

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__fill_composer: _AsyncOneStr = write_broker.fill_composer

    async def fill_composer(self, text: str) -> ActionResult:
        if self.action_type != "post":
            raise ScopedAuthorityDenied("action_mismatch")
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        self._require_live(PreparationVerb.FILL_COMPOSER)
        expected = self._expected_text()
        if text != expected:
            raise ScopedAuthorityDenied("payload_mismatch", "composer text differs")
        result = await self.__fill_composer(expected)
        if result.ok:
            self._mark_composer_opened()
            self._mark_text_filled()
        return result


class ReplyPreparationAuthority(_PreparationBase):
    __slots__ = ("__open_reply", "__fill_reply")

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__open_reply: _AsyncTwoStr = write_broker.open_reply_on_target
        self.__fill_reply: _AsyncOneStr = write_broker.fill_reply_composer

    async def open_reply_on_target(
        self, post_url: str, target_post_id: str
    ) -> ActionResult:
        if self.action_type != "reply":
            raise ScopedAuthorityDenied("action_mismatch")
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        if (
            post_url != self._expected_post_url()
            or target_post_id != self._expected_target_post_id()
        ):
            raise ScopedAuthorityDenied("target_mismatch")
        result = await self.__open_reply(
            self._expected_post_url(), self._expected_target_post_id()
        )
        if result.ok:
            self._mark_composer_opened()
        return result

    async def fill_reply_composer(self, text: str) -> ActionResult:
        self._require_live(PreparationVerb.FILL_COMPOSER)
        self._require_composer_opened()
        expected = self._expected_text()
        if text != expected:
            raise ScopedAuthorityDenied("payload_mismatch", "reply text differs")
        result = await self.__fill_reply(expected)
        if result.ok:
            self._mark_text_filled()
        return result


class QuotePreparationAuthority(_PreparationBase):
    __slots__ = ("__open_quote", "__fill_quote")

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__open_quote: _AsyncTwoStr = write_broker.open_quote_on_target
        self.__fill_quote: _AsyncOneStr = write_broker.fill_quote_composer

    async def open_quote_on_target(
        self, post_url: str, target_post_id: str
    ) -> ActionResult:
        if self.action_type != "quote":
            raise ScopedAuthorityDenied("action_mismatch")
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        if (
            post_url != self._expected_post_url()
            or target_post_id != self._expected_target_post_id()
        ):
            raise ScopedAuthorityDenied("target_mismatch")
        result = await self.__open_quote(
            self._expected_post_url(), self._expected_target_post_id()
        )
        if result.ok:
            self._mark_composer_opened()
        return result

    async def fill_quote_composer(self, text: str) -> ActionResult:
        self._require_live(PreparationVerb.FILL_COMPOSER)
        self._require_composer_opened()
        expected = self._expected_text()
        if text != expected:
            raise ScopedAuthorityDenied("payload_mismatch", "quote text differs")
        result = await self.__fill_quote(expected)
        if result.ok:
            self._mark_text_filled()
        return result


class _EffectAuthorityBase:
    __slots__ = ("__gateway", "__permit", "__binding", "__effect", "__invoke")

    def __init__(
        self,
        *,
        gateway: CommitGateway,
        permit: EffectPermit,
        binding: _IntentBinding,
        effect: EffectVerb,
        invoke: _EffectInvocation,
    ) -> None:
        self.__gateway = gateway
        self.__permit = permit
        self.__binding = binding
        self.__effect = effect
        self.__invoke = invoke

    @property
    def effect(self) -> EffectVerb:
        return self.__effect

    @property
    def consumed(self) -> bool:
        return self.__permit.consumed

    def _commit_gate(self) -> Optional[ActionResult]:
        """Consume exact permit at the concrete broker's final mutation seam."""
        binding = self.__binding
        try:
            self.__gateway.consume_permit(
                self.__permit,
                effect=self.__effect,
                intent_hash=binding.intent_hash,
                actor_id=binding.actor_id,
                target_type=binding.target_type,
                target_id=binding.target_id,
                policy_binding=binding.policy_binding,
            )
        except GatewayDenied as exc:
            return soft_failure(
                f"scoped commit denied: {exc.reason}",
                failure_category=FailureCategory.SECURITY,
            )
        return None

    async def _invoke_exact(self) -> ActionResult:
        return await self.__invoke(self._commit_gate)


class SetBookmarkAuthority(_EffectAuthorityBase):
    async def apply(self) -> ActionResult:
        return await self._invoke_exact()


class ClearBookmarkAuthority(_EffectAuthorityBase):
    async def apply(self) -> ActionResult:
        return await self._invoke_exact()


class SetLikeAuthority(_EffectAuthorityBase):
    async def apply(self) -> ActionResult:
        return await self._invoke_exact()


class ClearLikeAuthority(_EffectAuthorityBase):
    async def apply(self) -> ActionResult:
        return await self._invoke_exact()


class SubmitContentAuthority(_EffectAuthorityBase):
    async def submit(self) -> ActionResult:
        return await self._invoke_exact()


class DeletePostAuthority(_EffectAuthorityBase):
    async def delete(self) -> ActionResult:
        return await self._invoke_exact()


class ScopedAuthorityBroker:
    """Factory that never returns a caller-visible concrete broker."""

    __slots__ = ("__write_broker", "__gateway", "__policies")

    def __init__(
        self,
        write_broker: Any,
        commit_gateway: CommitGateway,
        *,
        policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
    ) -> None:
        self.__write_broker = write_broker
        self.__gateway = commit_gateway
        self.__policies = policies

    def _capture_current_binding(self, intent: WriteIntent) -> _IntentBinding:
        try:
            policy = self.__policies.require(intent.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        policy.validate()
        return _IntentBinding.capture(intent, policy_binding=policy.binding_hash())

    def prepare(
        self,
        *,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> PostPreparationAuthority | ReplyPreparationAuthority | QuotePreparationAuthority:
        binding = self._capture_current_binding(intent)
        try:
            policy = self.__policies.require(binding.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        if not policy.preparation_effects:
            raise ScopedAuthorityDenied("preparation_not_allowed", binding.action_type)

        if attempt.grant_id != grant.grant_id:
            raise ScopedAuthorityDenied("grant_mismatch")
        if grant.claimed_by != attempt.attempt_id:
            raise ScopedAuthorityDenied("claim_not_held")
        if attempt.state is not AttemptState.PREPARING:
            raise ScopedAuthorityDenied("attempt_not_preparing", attempt.state.value)
        if grant.action_type != binding.action_type:
            raise ScopedAuthorityDenied("action_mismatch")
        if grant.target_type != binding.target_type or grant.target_id != binding.target_id:
            raise ScopedAuthorityDenied("target_mismatch")
        try:
            grant.validate_live(
                intent_hash=binding.intent_hash,
                actor_id=binding.actor_id,
                policy_binding=binding.policy_binding,
                authorization_epoch=self.__gateway.authorization_epoch,
            )
        except GrantClaimDenied as exc:
            raise ScopedAuthorityDenied(exc.reason, str(exc)) from exc

        common: dict[str, Any] = {
            "write_broker": self.__write_broker,
            "grant": grant,
            "attempt": attempt,
            "binding": binding,
            "policies": self.__policies,
            "gateway": self.__gateway,
        }
        if binding.action_type == "post":
            return PostPreparationAuthority(**common)
        if binding.action_type == "reply":
            return ReplyPreparationAuthority(**common)
        if binding.action_type == "quote":
            return QuotePreparationAuthority(**common)
        raise ScopedAuthorityDenied("preparation_not_supported", binding.action_type)

    def authorize(
        self,
        *,
        permit: EffectPermit,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> _EffectAuthorityBase:
        binding = self._capture_current_binding(intent)
        if permit.intent_hash != binding.intent_hash:
            raise ScopedAuthorityDenied("intent_mismatch")
        if permit.actor_id != binding.actor_id:
            raise ScopedAuthorityDenied("actor_mismatch")
        if permit.action_type != binding.action_type:
            raise ScopedAuthorityDenied("action_mismatch")
        if permit.target_type != binding.target_type or permit.target_id != binding.target_id:
            raise ScopedAuthorityDenied("target_mismatch")
        if permit.policy_binding != binding.policy_binding:
            raise ScopedAuthorityDenied("policy_mismatch")
        if permit.attempt_id != attempt.attempt_id or permit.grant_id != attempt.grant_id:
            raise ScopedAuthorityDenied("attempt_mismatch")
        if permit.consumed:
            raise ScopedAuthorityDenied("permit_reused")

        expected = _EFFECT_BY_ACTION.get(binding.action_type)
        if expected is None:
            raise ScopedAuthorityDenied("effect_not_supported", binding.action_type)
        if permit.allowed_effects != frozenset({expected}):
            raise ScopedAuthorityDenied(
                "effect_scope_not_exact",
                f"expected only {expected.value}, got "
                f"{sorted(effect.value for effect in permit.allowed_effects)!r}",
            )

        broker = self.__write_broker
        cls: type[_EffectAuthorityBase]
        invoke: _EffectInvocation
        if expected is EffectVerb.SET_BOOKMARK:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_bookmark(binding.post_url, _commit_gate=gate)
            cls = SetBookmarkAuthority
        elif expected is EffectVerb.CLEAR_BOOKMARK:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_remove_bookmark(binding.post_url, _commit_gate=gate)
            cls = ClearBookmarkAuthority
        elif expected is EffectVerb.SET_LIKE:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_like(binding.post_url, _commit_gate=gate)
            cls = SetLikeAuthority
        elif expected is EffectVerb.CLEAR_LIKE:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_unlike(binding.post_url, _commit_gate=gate)
            cls = ClearLikeAuthority
        elif expected is EffectVerb.SUBMIT_CONTENT:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_submit(_commit_gate=gate)
            cls = SubmitContentAuthority
        elif expected is EffectVerb.DELETE_POST:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.delete_post(
                    binding.post_url,
                    binding.target_post_id,
                    _commit_gate=gate,
                )
            cls = DeletePostAuthority
        else:
            raise ScopedAuthorityDenied("effect_not_implemented", expected.value)

        return cls(
            gateway=self.__gateway,
            permit=permit,
            binding=binding,
            effect=expected,
            invoke=invoke,
        )
