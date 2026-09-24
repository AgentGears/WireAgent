"""M5 layer 4 — intent-bound preparation and delayed scoped effects.

Layer 4 is the process-local least-authority adapter between Layer 3's
CommitGateway and the concrete M5 write broker. Capability migration remains
Layer 5.

A scoped effect handle is deliberately *not* an EffectPermit. It can only ask the
CommitGateway to mint/reserve/spend at the broker's final mutation seam. This
keeps slow state probes, delete-menu staging, and composer preparation outside
the durable uncertainty window. The trusted Layer-5 orchestrator retains an
:class:`AuthorizedEffect` receipt; capability code receives only its narrow
``authority`` object.

Threat model: same-process engineering boundary against accidental overreach,
not a hostile-Python sandbox. Untrusted code still requires process/OS isolation
and no raw browser handle.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Awaitable, Callable
from copy import deepcopy
from dataclasses import dataclass, field
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
    "AuthorizedEffect",
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
_CONTENT_ACTIONS = frozenset({"post", "reply", "quote"})
_STATUS_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_STATUS_PATH_RE = re.compile(r"/status/(\d+)(?:/|$)")
_SHA256_RE = re.compile(r"[0-9a-fA-F]{64}")


class ScopedAuthorityDenied(RuntimeError):
    """A scoped-authority construction or use request violated its binding."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"scoped authority denied: {reason}" + (f" — {detail}" if detail else "")
        )


def _bind_status_url(raw_url: str, target_post_id: str) -> str:
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
    def capture_frozen(
        cls,
        frozen: WriteIntent,
        *,
        policy_binding: str,
    ) -> "_IntentBinding":
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
            raise ScopedAuthorityDenied("target_mismatch")
        if frozen.action_type in _POST_TARGET_ACTIONS and target_post_id != target_id:
            raise ScopedAuthorityDenied("target_mismatch")

        post_url = post_url_raw
        if frozen.action_type in _POST_TARGET_ACTIONS:
            post_url = _bind_status_url(post_url_raw, target_post_id)
        elif not post_url and target_post_id:
            post_url = f"https://x.com/i/status/{target_post_id}"

        text = payload.get("normalized_text", "")
        if text is None:
            text = ""
        if not isinstance(text, str):
            raise ScopedAuthorityDenied("payload_invalid", "normalized_text must be a string")

        raw_media = payload.get("manifest_items", [])
        if raw_media is None:
            raw_media = []
        if not isinstance(raw_media, list):
            raise ScopedAuthorityDenied("payload_invalid", "manifest_items must be a list")
        media: list[_MediaBinding] = []
        for expected_index, item in enumerate(raw_media):
            if not isinstance(item, dict):
                raise ScopedAuthorityDenied("payload_invalid", "manifest item must be a dict")
            index = item.get("index")
            source_path = item.get("source_path")
            digest = item.get("sha256")
            if index != expected_index:
                raise ScopedAuthorityDenied("media_order_mismatch")
            if not isinstance(source_path, str) or not source_path:
                raise ScopedAuthorityDenied("payload_invalid", "manifest path is invalid")
            if not isinstance(digest, str) or _SHA256_RE.fullmatch(digest) is None:
                raise ScopedAuthorityDenied("payload_invalid", "manifest digest is invalid")
            media.append(_MediaBinding(expected_index, source_path, digest.lower()))

        return cls(
            action_type=frozen.action_type,
            target_type=target_type,
            target_id=target_id,
            actor_id=actor_id,
            intent_hash=frozen.intent_hash(),
            policy_binding=policy_binding,
            post_url=post_url,
            target_post_id=target_post_id,
            normalized_text=text,
            media=tuple(media),
        )


@dataclass
class _PreparationTracker:
    intent_hash: str
    action_type: str
    target_id: str
    expected_media: int
    composer_opened: bool = False
    text_filled: bool = False
    media_attached: int = 0
    sealed: bool = False
    in_flight: bool = False
    cleanup_in_flight: bool = False
    revision: int = 0
    _lock: Any = field(default_factory=threading.RLock, repr=False, compare=False)

    def _require_binding_unlocked(self, binding: _IntentBinding) -> None:
        if (
            self.intent_hash != binding.intent_hash
            or self.action_type != binding.action_type
            or self.target_id != binding.target_id
            or self.expected_media != len(binding.media)
        ):
            raise ScopedAuthorityDenied("preparation_binding_mismatch")

    def require_binding(self, binding: _IntentBinding) -> None:
        with self._lock:
            self._require_binding_unlocked(binding)

    def is_sealed(self) -> bool:
        with self._lock:
            return self.sealed

    def cleanup_active(self) -> bool:
        with self._lock:
            return self.cleanup_in_flight

    def ready(self, binding: _IntentBinding) -> bool:
        with self._lock:
            self._require_binding_unlocked(binding)
            return (
                not self.in_flight
                and not self.cleanup_in_flight
                and self.composer_opened
                and self.text_filled
                and self.media_attached == self.expected_media
            )

    def ready_and_sealed(self, binding: _IntentBinding) -> bool:
        with self._lock:
            self._require_binding_unlocked(binding)
            return (
                self.sealed
                and not self.in_flight
                and not self.cleanup_in_flight
                and self.composer_opened
                and self.text_filled
                and self.media_attached == self.expected_media
            )

    def seal(self, binding: _IntentBinding) -> None:
        with self._lock:
            self._require_binding_unlocked(binding)
            if self.sealed:
                raise ScopedAuthorityDenied("preparation_sealed")
            if (
                self.in_flight
                or self.cleanup_in_flight
                or not self.composer_opened
                or not self.text_filled
                or self.media_attached != self.expected_media
            ):
                raise ScopedAuthorityDenied("preparation_incomplete")
            self.sealed = True

    def begin(self, binding: _IntentBinding) -> int:
        with self._lock:
            self._require_binding_unlocked(binding)
            if self.sealed:
                raise ScopedAuthorityDenied("preparation_sealed")
            if self.in_flight:
                raise ScopedAuthorityDenied("preparation_in_flight")
            if self.cleanup_in_flight:
                raise ScopedAuthorityDenied("preparation_cleanup_in_flight")
            self.in_flight = True
            return self.revision

    def finish(
        self,
        revision: int,
        *,
        succeeded: bool,
        composer_opened: bool = False,
        text_filled: bool = False,
        media_delta: int = 0,
    ) -> bool:
        with self._lock:
            valid = (
                self.in_flight
                and not self.cleanup_in_flight
                and self.revision == revision
                and not self.sealed
            )
            if valid and succeeded:
                if composer_opened:
                    self.composer_opened = True
                if text_filled:
                    self.text_filled = True
                self.media_attached += media_delta
            self.in_flight = False
            return valid

    def begin_cleanup(self) -> None:
        with self._lock:
            if self.cleanup_in_flight:
                raise ScopedAuthorityDenied("preparation_cleanup_in_flight")
            self.cleanup_in_flight = True
            self.revision += 1
            self.composer_opened = False
            self.text_filled = False
            self.media_attached = 0

    def finish_cleanup(self) -> None:
        with self._lock:
            self.cleanup_in_flight = False


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


@dataclass
class _PermitHolder:
    permit: Optional[EffectPermit] = None


class _PreparationBase:
    __slots__ = (
        "__grant",
        "__attempt",
        "__binding",
        "__policies",
        "__gateway",
        "__tracker",
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
        tracker: _PreparationTracker,
    ) -> None:
        self.__grant = grant
        self.__attempt = attempt
        self.__binding = binding
        self.__policies = policies
        self.__gateway = gateway
        self.__tracker = tracker
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

    def _tracker(self) -> _PreparationTracker:
        return self.__tracker

    def _binding(self) -> _IntentBinding:
        return self.__binding

    def _expected_text(self) -> str:
        return self.__binding.normalized_text

    def _expected_post_url(self) -> str:
        return self.__binding.post_url

    def _expected_target_post_id(self) -> str:
        return self.__binding.target_post_id

    def _require_live(self, verb: PreparationVerb) -> None:
        tracker = self.__tracker
        if tracker.is_sealed():
            raise ScopedAuthorityDenied("preparation_sealed")
        if tracker.cleanup_active():
            raise ScopedAuthorityDenied("preparation_cleanup_in_flight")
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
        tracker = self.__tracker
        if not tracker.text_filled:
            raise ScopedAuthorityDenied("preparation_order")
        if tracker.media_attached >= len(self.__binding.media):
            raise ScopedAuthorityDenied("media_not_approved", image_path)
        expected = self.__binding.media[tracker.media_attached]
        if image_path != expected.source_path:
            raise ScopedAuthorityDenied("media_order_mismatch")
        try:
            digest = file_sha256(Path(expected.source_path)).lower()
        except OSError as exc:
            raise ScopedAuthorityDenied("media_unreadable", str(exc)) from exc
        if digest != expected.sha256:
            raise ScopedAuthorityDenied("media_changed_after_approval")
        self._require_live(PreparationVerb.ATTACH_MEDIA)
        revision = tracker.begin(self.__binding)
        try:
            result = await self.__attach_media(expected.source_path)
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(revision, succeeded=result.ok, media_delta=1)
        return result

    async def close_composer(self) -> ActionResult:
        tracker = self.__tracker
        tracker.begin_cleanup()
        try:
            return await self.__close_composer()
        finally:
            tracker.finish_cleanup()


class PostPreparationAuthority(_PreparationBase):
    __slots__ = ("__fill_composer",)

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__fill_composer: _AsyncOneStr = write_broker.fill_composer

    async def fill_composer(self, text: str) -> ActionResult:
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        self._require_live(PreparationVerb.FILL_COMPOSER)
        tracker = self._tracker()
        if tracker.text_filled or tracker.media_attached:
            raise ScopedAuthorityDenied("preparation_order")
        if text != self._expected_text():
            raise ScopedAuthorityDenied("payload_mismatch")
        revision = tracker.begin(self._binding())
        try:
            result = await self.__fill_composer(self._expected_text())
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(
            revision,
            succeeded=result.ok,
            composer_opened=True,
            text_filled=True,
        )
        return result


class ReplyPreparationAuthority(_PreparationBase):
    __slots__ = ("__open_reply", "__fill_reply")

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__open_reply: _AsyncTwoStr = write_broker.open_reply_on_target
        self.__fill_reply: _AsyncOneStr = write_broker.fill_reply_composer

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        tracker = self._tracker()
        if tracker.composer_opened or tracker.text_filled or tracker.media_attached:
            raise ScopedAuthorityDenied("preparation_order")
        if post_url != self._expected_post_url() or target_post_id != self._expected_target_post_id():
            raise ScopedAuthorityDenied("target_mismatch")
        revision = tracker.begin(self._binding())
        try:
            result = await self.__open_reply(
                self._expected_post_url(), self._expected_target_post_id()
            )
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(revision, succeeded=result.ok, composer_opened=True)
        return result

    async def fill_reply_composer(self, text: str) -> ActionResult:
        self._require_live(PreparationVerb.FILL_COMPOSER)
        tracker = self._tracker()
        if not tracker.composer_opened or tracker.text_filled:
            raise ScopedAuthorityDenied("preparation_order")
        if text != self._expected_text():
            raise ScopedAuthorityDenied("payload_mismatch")
        revision = tracker.begin(self._binding())
        try:
            result = await self.__fill_reply(self._expected_text())
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(revision, succeeded=result.ok, text_filled=True)
        return result


class QuotePreparationAuthority(_PreparationBase):
    __slots__ = ("__open_quote", "__fill_quote")

    def __init__(self, *, write_broker: Any, **kwargs: Any) -> None:
        super().__init__(write_broker=write_broker, **kwargs)
        self.__open_quote: _AsyncTwoStr = write_broker.open_quote_on_target
        self.__fill_quote: _AsyncOneStr = write_broker.fill_quote_composer

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        self._require_live(PreparationVerb.OPEN_COMPOSER)
        tracker = self._tracker()
        if tracker.composer_opened or tracker.text_filled or tracker.media_attached:
            raise ScopedAuthorityDenied("preparation_order")
        if post_url != self._expected_post_url() or target_post_id != self._expected_target_post_id():
            raise ScopedAuthorityDenied("target_mismatch")
        revision = tracker.begin(self._binding())
        try:
            result = await self.__open_quote(
                self._expected_post_url(), self._expected_target_post_id()
            )
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(revision, succeeded=result.ok, composer_opened=True)
        return result

    async def fill_quote_composer(self, text: str) -> ActionResult:
        self._require_live(PreparationVerb.FILL_COMPOSER)
        tracker = self._tracker()
        if not tracker.composer_opened or tracker.text_filled:
            raise ScopedAuthorityDenied("preparation_order")
        if text != self._expected_text():
            raise ScopedAuthorityDenied("payload_mismatch")
        revision = tracker.begin(self._binding())
        try:
            result = await self.__fill_quote(self._expected_text())
        except BaseException:
            tracker.finish(revision, succeeded=False)
            raise
        tracker.finish(revision, succeeded=result.ok, text_filled=True)
        return result


class _EffectAuthorityBase:
    __slots__ = (
        "__gateway",
        "__grant",
        "__attempt",
        "__frozen_intent",
        "__binding",
        "__effect",
        "__invoke",
        "__holder",
    )

    def __init__(
        self,
        *,
        gateway: CommitGateway,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        frozen_intent: WriteIntent,
        binding: _IntentBinding,
        effect: EffectVerb,
        invoke: _EffectInvocation,
        holder: _PermitHolder,
    ) -> None:
        self.__gateway = gateway
        self.__grant = grant
        self.__attempt = attempt
        self.__frozen_intent = frozen_intent
        self.__binding = binding
        self.__effect = effect
        self.__invoke = invoke
        self.__holder = holder

    @property
    def effect(self) -> EffectVerb:
        return self.__effect

    @property
    def consumed(self) -> bool:
        permit = self.__holder.permit
        return bool(permit is not None and permit.consumed)

    def _commit_gate(self) -> Optional[ActionResult]:
        binding = self.__binding
        permit = self.__holder.permit
        try:
            if permit is None:
                permit = self.__gateway.authorize_commit(
                    grant=self.__grant,
                    attempt=self.__attempt,
                    intent=self.__frozen_intent,
                )
                self.__holder.permit = permit
            self.__gateway.consume_permit(
                permit,
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


EffectAuthority = (
    SetBookmarkAuthority
    | ClearBookmarkAuthority
    | SetLikeAuthority
    | ClearLikeAuthority
    | SubmitContentAuthority
    | DeletePostAuthority
)


@dataclass(frozen=True)
class AuthorizedEffect:
    """Trusted-orchestrator receipt; capability code receives only authority."""

    authority: EffectAuthority
    attempt: EffectAttempt
    _holder: _PermitHolder

    @property
    def permit(self) -> Optional[EffectPermit]:
        """Exact permit after the mutation boundary is attempted, else ``None``."""
        return self._holder.permit


class ScopedAuthorityBroker:
    __slots__ = ("__write_broker", "__gateway", "__policies", "__preparations")

    def __init__(
        self,
        write_broker: Any,
        commit_gateway: CommitGateway,
        *,
        policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
    ) -> None:
        gateway_policies = getattr(commit_gateway, "_policies", None)
        if gateway_policies is not policies:
            raise ScopedAuthorityDenied("policy_registry_mismatch")
        self.__write_broker = write_broker
        self.__gateway = commit_gateway
        self.__policies = policies
        self.__preparations: dict[str, _PreparationTracker] = {}

    def _freeze_binding(self, intent: WriteIntent) -> tuple[WriteIntent, _IntentBinding]:
        frozen = deepcopy(intent)
        try:
            policy = self.__policies.require(frozen.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        policy.validate()
        binding = _IntentBinding.capture_frozen(frozen, policy_binding=policy.binding_hash())
        self._require_exact_effect_scope(binding)
        return frozen, binding

    def _require_exact_effect_scope(self, binding: _IntentBinding) -> EffectVerb:
        expected = _EFFECT_BY_ACTION.get(binding.action_type)
        if expected is None:
            raise ScopedAuthorityDenied("effect_not_supported", binding.action_type)
        try:
            policy = self.__policies.require(binding.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        if policy.allowed_effects != frozenset({expected}):
            raise ScopedAuthorityDenied("effect_scope_not_exact")
        return expected

    @staticmethod
    def _validate_grant_for_binding(
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        binding: _IntentBinding,
        *,
        authorization_epoch: int,
    ) -> None:
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
            grant.validate_live(
                intent_hash=binding.intent_hash,
                actor_id=binding.actor_id,
                policy_binding=binding.policy_binding,
                authorization_epoch=authorization_epoch,
            )
        except GrantClaimDenied as exc:
            raise ScopedAuthorityDenied(exc.reason, str(exc)) from exc

    def prepare(
        self,
        *,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> PostPreparationAuthority | ReplyPreparationAuthority | QuotePreparationAuthority:
        _, binding = self._freeze_binding(intent)
        try:
            policy = self.__policies.require(binding.action_type)
        except KeyError as exc:
            raise ScopedAuthorityDenied("policy_missing", str(exc)) from exc
        if binding.action_type not in _CONTENT_ACTIONS or not policy.preparation_effects:
            raise ScopedAuthorityDenied("preparation_not_allowed", binding.action_type)
        self._validate_grant_for_binding(
            grant,
            attempt,
            binding,
            authorization_epoch=self.__gateway.authorization_epoch,
        )

        tracker = self.__preparations.get(attempt.attempt_id)
        if tracker is None:
            tracker = _PreparationTracker(
                binding.intent_hash,
                binding.action_type,
                binding.target_id,
                len(binding.media),
            )
            self.__preparations[attempt.attempt_id] = tracker
        else:
            tracker.require_binding(binding)
            if tracker.is_sealed():
                raise ScopedAuthorityDenied("preparation_sealed")
            if tracker.cleanup_active():
                raise ScopedAuthorityDenied("preparation_cleanup_in_flight")

        common: dict[str, Any] = {
            "write_broker": self.__write_broker,
            "grant": grant,
            "attempt": attempt,
            "binding": binding,
            "policies": self.__policies,
            "gateway": self.__gateway,
            "tracker": tracker,
        }
        if binding.action_type == "post":
            return PostPreparationAuthority(**common)
        if binding.action_type == "reply":
            return ReplyPreparationAuthority(**common)
        if binding.action_type == "quote":
            return QuotePreparationAuthority(**common)
        raise ScopedAuthorityDenied("preparation_not_supported", binding.action_type)

    def scope_effect(
        self,
        *,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> AuthorizedEffect:
        """Create a narrow handle; mint the EffectPermit only at its commit gate."""
        frozen, binding = self._freeze_binding(intent)
        self._validate_grant_for_binding(
            grant,
            attempt,
            binding,
            authorization_epoch=self.__gateway.authorization_epoch,
        )
        expected = self._require_exact_effect_scope(binding)

        tracker: Optional[_PreparationTracker] = None
        if binding.action_type in _CONTENT_ACTIONS:
            tracker = self.__preparations.get(attempt.attempt_id)
            if tracker is None:
                raise ScopedAuthorityDenied("preparation_incomplete")
            tracker.seal(binding)

        broker = self.__write_broker
        holder = _PermitHolder()
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
            assert tracker is not None

            async def precommit_check() -> Optional[ActionResult]:
                if not tracker.ready_and_sealed(binding):
                    return soft_failure(
                        "approved preparation state is not sealed/complete",
                        failure_category=FailureCategory.SECURITY,
                    )
                text_r = await broker.read_composer_text()
                if not text_r.ok:
                    return text_r
                if (text_r.data or {}).get("composer_text", "") != binding.normalized_text:
                    return soft_failure(
                        "composer text no longer matches approved intent",
                        failure_category=FailureCategory.SECURITY,
                    )
                count_r = await broker.count_attachments()
                if not count_r.ok:
                    return count_r
                if (count_r.data or {}).get("count") != len(binding.media):
                    return soft_failure(
                        "composer attachment count no longer matches approved intent",
                        failure_category=FailureCategory.SECURITY,
                    )
                if binding.media:
                    ready_r = await broker.verify_attachment_ready()
                    if not ready_r.ok:
                        return ready_r
                    if (ready_r.data or {}).get("ready") is not True:
                        return soft_failure(
                            "approved attachment is not ready at submit boundary",
                            failure_category=FailureCategory.SECURITY,
                        )
                return None

            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.click_submit(
                    _commit_gate=gate,
                    _precommit_check=precommit_check,
                )
            cls = SubmitContentAuthority
        elif expected is EffectVerb.DELETE_POST:
            async def invoke(gate: _CommitGate) -> ActionResult:
                return await broker.delete_post(
                    binding.post_url,
                    binding.target_post_id,
                    _commit_gate=gate,
                )
            cls = DeletePostAuthority
        else:  # pragma: no cover
            raise ScopedAuthorityDenied("effect_not_implemented", expected.value)

        authority = cls(
            gateway=self.__gateway,
            grant=grant,
            attempt=attempt,
            frozen_intent=frozen,
            binding=binding,
            effect=expected,
            invoke=invoke,
            holder=holder,
        )
        return AuthorizedEffect(authority=authority, attempt=attempt, _holder=holder)
