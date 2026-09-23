"""M5 effect policy — independent risk, authority, replay, and durability axes.

This module describes what an action is allowed to do and what crash/replay
semantics the runtime may rely on. It deliberately does not execute browser
mutations.

A BEST_EFFORT durability assignment is therefore a positive claim: replay has
been established to create no additional meaningful external effect. Explicitly
recorded unknown outcomes still require reconciliation; the special case is a
process crash before a BEST_EFFORT terminal record exists, where replay safety
is the only available guarantee.

Source of truth: docs/M5_DESIGN.md §4.
"""

from __future__ import annotations

import hashlib
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import Iterable, Iterator, Optional

from webwire.safety.models import RiskTier
from webwire.safety.risk_registry import DEFAULT_REGISTRY, RiskRegistry

__all__ = [
    "DurabilityPolicy",
    "EffectPolicy",
    "EffectPolicyRegistry",
    "EffectVerb",
    "ReplaySemantics",
    "DEFAULT_EFFECT_POLICIES",
    "derive_durability",
]


class ReplaySemantics(StrEnum):
    """Replay behavior of one semantic effect, independent of impact/risk."""

    SAFE_STATE_SET = "safe_state_set"
    SAFE_TARGET_DELETE = "safe_target_delete"
    NON_IDEMPOTENT_CREATE = "non_idempotent_create"
    REPLAY_HAS_RESIDUAL_EFFECTS = "replay_has_residual_effects"
    UNKNOWN = "unknown"


class DurabilityPolicy(StrEnum):
    """Whether commit authority requires an fsync-backed reservation."""

    REQUIRED = "required"
    BEST_EFFORT = "best_effort"


class EffectVerb(StrEnum):
    """Semantic authority verbs. These are not generic DOM primitives."""

    SET_BOOKMARK = "set_bookmark"
    CLEAR_BOOKMARK = "clear_bookmark"
    SET_LIKE = "set_like"
    CLEAR_LIKE = "clear_like"
    FOLLOW = "follow"
    UNFOLLOW = "unfollow"
    REPOST = "repost"
    UNREPOST = "unrepost"
    OPEN_COMPOSER = "open_composer"
    FILL_COMPOSER = "fill_composer"
    ATTACH_MEDIA = "attach_media"
    SUBMIT_CONTENT = "submit_content"
    DELETE_POST = "delete_post"


def derive_durability(
    risk_tier: RiskTier,
    replay_semantics: ReplaySemantics,
) -> DurabilityPolicy:
    """Derive fencing from risk and replay semantics."""
    if risk_tier in {
        RiskTier.PUBLIC_AMPLIFYING_REVERSIBLE,
        RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
    }:
        return DurabilityPolicy.REQUIRED

    if replay_semantics in {
        ReplaySemantics.NON_IDEMPOTENT_CREATE,
        ReplaySemantics.REPLAY_HAS_RESIDUAL_EFFECTS,
        ReplaySemantics.UNKNOWN,
    }:
        return DurabilityPolicy.REQUIRED

    return DurabilityPolicy.BEST_EFFORT


@dataclass(frozen=True)
class EffectPolicy:
    """Registry-owned declaration for one action type."""

    action_type: str
    risk_tier: RiskTier
    allowed_effects: frozenset[EffectVerb]
    replay_semantics: ReplaySemantics
    durability: DurabilityPolicy
    schema_version: int = 1

    @classmethod
    def derive(
        cls,
        *,
        action_type: str,
        risk_tier: RiskTier,
        allowed_effects: Iterable[EffectVerb],
        replay_semantics: ReplaySemantics,
    ) -> "EffectPolicy":
        effects = frozenset(allowed_effects)
        if not effects:
            raise ValueError(
                f"effect policy {action_type!r} must allow at least one effect"
            )
        return cls(
            action_type=action_type,
            risk_tier=risk_tier,
            allowed_effects=effects,
            replay_semantics=replay_semantics,
            durability=derive_durability(risk_tier, replay_semantics),
        )

    def validate(self) -> None:
        """Fail loud on policy drift or attempted durability downgrade."""
        if not self.action_type:
            raise ValueError("effect policy action_type must not be empty")
        if not self.allowed_effects:
            raise ValueError(
                f"effect policy {self.action_type!r} has no allowed effects"
            )
        expected = derive_durability(self.risk_tier, self.replay_semantics)
        if self.durability != expected:
            raise ValueError(
                f"effect policy {self.action_type!r} durability "
                f"{self.durability.value!r} does not match derived requirement "
                f"{expected.value!r}"
            )

    def binding_hash(self) -> str:
        """Stable identity bound into ApprovalGrants and EffectPermits."""
        payload = {
            "schema_version": self.schema_version,
            "action_type": self.action_type,
            "risk_tier": self.risk_tier.value,
            "allowed_effects": sorted(effect.value for effect in self.allowed_effects),
            "replay_semantics": self.replay_semantics.value,
            "durability": self.durability.value,
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class EffectPolicyRegistry:
    """Authoritative, thread-safe action_type -> EffectPolicy map."""

    def __init__(self) -> None:
        self._entries: dict[str, EffectPolicy] = {}
        self._lock = threading.RLock()

    def register(self, policy: EffectPolicy) -> None:
        policy.validate()
        with self._lock:
            self._entries[policy.action_type] = policy

    def get(self, action_type: str) -> Optional[EffectPolicy]:
        with self._lock:
            return self._entries.get(action_type)

    def require(self, action_type: str) -> EffectPolicy:
        with self._lock:
            policy = self._entries.get(action_type)
            if policy is None:
                raise KeyError(f"action_type {action_type!r} has no effect policy")
            return policy

    def known_actions(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    @contextmanager
    def policy_fence(self, action_type: str) -> Iterator[EffectPolicy]:
        """Hold registry identity stable across one authority transition."""
        with self._lock:
            policy = self._entries.get(action_type)
            if policy is None:
                raise KeyError(f"action_type {action_type!r} has no effect policy")
            policy.validate()
            yield policy


def _build_default(risk_registry: RiskRegistry = DEFAULT_REGISTRY) -> EffectPolicyRegistry:
    """Build initial M5 policy truth from implemented evidence plus safe defaults."""
    reg = EffectPolicyRegistry()

    def add(
        action_type: str,
        replay: ReplaySemantics,
        effects: Iterable[EffectVerb],
    ) -> None:
        risk_meta, _ = risk_registry.require(action_type)
        reg.register(
            EffectPolicy.derive(
                action_type=action_type,
                risk_tier=risk_meta.derive_tier(),
                allowed_effects=effects,
                replay_semantics=replay,
            )
        )

    # Bookmark directions are state-first in the concrete WriteBroker and have
    # broker-level regressions covering selector coexistence and zero-mutation
    # already-satisfied behavior, so SAFE_STATE_SET is evidence-backed here.
    add("bookmark", ReplaySemantics.SAFE_STATE_SET, {EffectVerb.SET_BOOKMARK})
    add(
        "remove_bookmark",
        ReplaySemantics.SAFE_STATE_SET,
        {EffectVerb.CLEAR_BOOKMARK},
    )

    # LikeCapability performs a high-level pre-state read, but the concrete
    # click_like/click_unlike broker methods remain selector-first and lack the
    # broker-level semantic regressions required to prove replay safety under
    # selector coexistence/DOM churn. Until that evidence lands, UNKNOWN keeps
    # both directions durably fenced rather than weakening safety by assertion.
    add("like", ReplaySemantics.UNKNOWN, {EffectVerb.SET_LIKE})
    add("unlike", ReplaySemantics.UNKNOWN, {EffectVerb.CLEAR_LIKE})

    # Future mutation families have no real broker implementation yet. Their
    # replay behavior is UNKNOWN until an implementation + regression proves a
    # stronger contract. This intentionally keeps them durably fenced.
    add("follow", ReplaySemantics.UNKNOWN, {EffectVerb.FOLLOW})
    add("unfollow", ReplaySemantics.UNKNOWN, {EffectVerb.UNFOLLOW})
    add("repost", ReplaySemantics.UNKNOWN, {EffectVerb.REPOST})
    add("unrepost", ReplaySemantics.UNKNOWN, {EffectVerb.UNREPOST})

    content_effects = {
        EffectVerb.OPEN_COMPOSER,
        EffectVerb.FILL_COMPOSER,
        EffectVerb.ATTACH_MEDIA,
        EffectVerb.SUBMIT_CONTENT,
    }
    add("post", ReplaySemantics.NON_IDEMPOTENT_CREATE, content_effects)
    add("reply", ReplaySemantics.NON_IDEMPOTENT_CREATE, content_effects)
    add("quote", ReplaySemantics.NON_IDEMPOTENT_CREATE, content_effects)
    add("delete_post", ReplaySemantics.SAFE_TARGET_DELETE, {EffectVerb.DELETE_POST})

    return reg


DEFAULT_EFFECT_POLICIES = _build_default()
