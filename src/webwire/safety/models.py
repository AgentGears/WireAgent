"""Write-safety kernel data models (Phase 0b).

The hardened design (review conversation 6a4fb320) centers on:
- WriteIntent: declarative description of what a write WILL do. Policy evaluates
  on intent, before any browser mutation.
- RiskMeta: multi-dimensional risk metadata (visibility, reversibility,
  amplification, etc.) from which a RiskTier is derived.
- ConfirmationToken: bound to one capability and one immutable intent hash; not
  a bare boolean and not transferable across equal-intent capability surfaces.
- PolicyDecision: the policy stage's verdict (allow/deny/dry_run/confirmation_required).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Optional


class RiskTier(StrEnum):
    """Derived policy tiers from RiskMeta dimensions. Per review Q4 — bookmark
    and like are NOT the same tier (bookmark is private; like is public engagement)."""
    PRIVATE_REVERSIBLE = "private_reversible"                # bookmark
    PUBLIC_REVERSIBLE_ENGAGEMENT = "public_reversible_engagement"  # like, follow
    PUBLIC_AMPLIFYING_REVERSIBLE = "public_amplifying_reversible"  # repost/retweet
    PUBLIC_CONTENT_IRREVERSIBLE = "public_content_irreversible"    # post, reply, quote


class Visibility(StrEnum):
    PRIVATE = "private"
    SEMI_PUBLIC = "semi_public"
    PUBLIC = "public"


class Reversibility(StrEnum):
    REVERSIBLE = "reversible"
    COMPENSATABLE = "compensatable"  # can delete, but side effects persist
    IRREVERSIBLE = "irreversible"


class Amplification(StrEnum):
    NONE = "none"
    ENGAGEMENT_SIGNAL = "engagement_signal"  # like, follow
    BROADCAST = "broadcast"                   # repost, post


@dataclass(frozen=True)
class RiskMeta:
    """Multi-dimensional risk metadata for an action type. The RiskTier is
    derived from these dimensions, not hardcoded per action."""
    visibility: Visibility
    reversibility: Reversibility
    amplification: Amplification
    content_creation: bool = False  # creates user content (post/reply) vs signal (like)
    residual_side_effects: tuple[str, ...] = ()

    def derive_tier(self) -> RiskTier:
        """Derive the policy tier from the metadata dimensions.

        Order matters: content_creation is the strongest signal (posts/replies/
        quotes are the highest-risk tier), checked before amplification, because
        a post is both content_creation AND broadcast but should land in the
        IRREVERSIBLE tier, not the AMPLIFYING one."""
        # Content creation (post/reply/quote) is always the highest tier, even
        # if technically compensatable — deletion is not true undo.
        if self.content_creation:
            return RiskTier.PUBLIC_CONTENT_IRREVERSIBLE
        if self.amplification == Amplification.BROADCAST:
            return RiskTier.PUBLIC_AMPLIFYING_REVERSIBLE
        if self.visibility == Visibility.PUBLIC and self.amplification == Amplification.ENGAGEMENT_SIGNAL:
            return RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT
        if self.visibility == Visibility.PRIVATE and self.reversibility == Reversibility.REVERSIBLE:
            return RiskTier.PRIVATE_REVERSIBLE
        # Conservative default: treat unknown combinations as the highest risk.
        return RiskTier.PUBLIC_CONTENT_IRREVERSIBLE


@dataclass(frozen=True)
class CompensationMeta:
    """Per-action compensation metadata. NEVER called 'undo' for public actions
    (review final rec #5) — deletion is compensation, not true undo."""
    supports_compensation: bool
    compensation_action: Optional[str] = None  # e.g. "unlike", "remove_bookmark", "delete_post"
    residual_side_effects: tuple[str, ...] = ()


@dataclass
class WriteIntent:
    """Declarative description of what a write WILL do. Produced by compose().
    The kernel evaluates policy on this BEFORE any browser mutation."""
    action_type: str                    # e.g. "like", "bookmark", "post"
    target_type: str                    # e.g. "post", "user"
    target_id: str                      # e.g. post_id or handle
    risk_meta: RiskMeta
    compensation: CompensationMeta
    # Semantic variant for dedupe (review Q3): e.g. reply text hash, or "" for toggles.
    semantic_variant: str = ""
    # Arbitrary action-specific payload (e.g. post text for compose, but NOT for like).
    payload: dict[str, Any] = field(default_factory=dict)
    # The actor identity (whoami handle) — included in dedupe key so multi-account
    # doesn't collide (though Phase 0b is single-account).
    actor_identity: Optional[str] = None

    def dedupe_key(self) -> str:
        """Canonical semantic dedupe key (review Q3)."""
        return f"{self.actor_identity or '?'}|{self.action_type}|{self.target_type}|{self.target_id}|{self.semantic_variant}"

    def intent_hash(self) -> str:
        """Stable hash of the full intent for confirmation-token binding (review Q1).
        Canonicalizes the intent fields so a materially different intent produces
        a different hash."""
        canonical = "|".join([
            self.action_type,
            self.target_type,
            self.target_id,
            self.dedupe_key(),
            str(sorted(self.payload.items())),
        ])
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    def risk_tier(self) -> RiskTier:
        return self.risk_meta.derive_tier()


@dataclass
class ConfirmationToken:
    """Bound to one capability + immutable intent hash. Single-use, expiring."""

    token: str
    intent_hash: str
    risk_tier: RiskTier
    capability_name: str = ""
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # set by kernel; default 0 = invalid until set
    consumed: bool = False

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return t >= self.expires_at


class PolicyVerdict(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    DRY_RUN = "dry_run"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass
class PolicyDecision:
    """The policy stage's verdict + the reason."""
    verdict: PolicyVerdict
    reason: str
    risk_tier: RiskTier
    intent_hash: str
    # Set when verdict == CONFIRMATION_REQUIRED.
    confirmation_token: Optional[ConfirmationToken] = None
    # Set when verdict == DENY (which gate blocked).
    blocked_by: Optional[str] = None  # "kill_switch" | "dedupe" | "token_bucket" | "risk_tier" | "unknown_action" | "risk_meta_mismatch" | "reconciliation_required" | "expired_token" | "intent_mismatch" | "capability_mismatch" | "consumed_token"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d
