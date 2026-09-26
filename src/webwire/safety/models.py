"""Write-safety kernel data models (Phase 0b / M6 confirmation hardening).

The hardened design centers on:
- WriteIntent: declarative description of what a write WILL do. Policy evaluates
  on intent, before any browser mutation.
- RiskMeta: multi-dimensional risk metadata (visibility, reversibility,
  amplification, etc.) from which a RiskTier is derived.
- ConfirmationToken: bound to one capability, immutable intent hash, M6
  confirmation epoch, and monotonic authority lifetime.
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
        if self.content_creation:
            return RiskTier.PUBLIC_CONTENT_IRREVERSIBLE
        if self.amplification == Amplification.BROADCAST:
            return RiskTier.PUBLIC_AMPLIFYING_REVERSIBLE
        if self.visibility == Visibility.PUBLIC and self.amplification == Amplification.ENGAGEMENT_SIGNAL:
            return RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT
        if self.visibility == Visibility.PRIVATE and self.reversibility == Reversibility.REVERSIBLE:
            return RiskTier.PRIVATE_REVERSIBLE
        return RiskTier.PUBLIC_CONTENT_IRREVERSIBLE


@dataclass(frozen=True)
class CompensationMeta:
    """Per-action compensation metadata. NEVER called 'undo' for public actions."""
    supports_compensation: bool
    compensation_action: Optional[str] = None
    residual_side_effects: tuple[str, ...] = ()


@dataclass
class WriteIntent:
    """Declarative description of what a write WILL do. Produced by compose()."""
    action_type: str
    target_type: str
    target_id: str
    risk_meta: RiskMeta
    compensation: CompensationMeta
    semantic_variant: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    actor_identity: Optional[str] = None

    def dedupe_key(self) -> str:
        """Canonical semantic dedupe key (review Q3)."""
        return f"{self.actor_identity or '?'}|{self.action_type}|{self.target_type}|{self.target_id}|{self.semantic_variant}"

    def intent_hash(self) -> str:
        """Stable hash of the full intent for confirmation-token binding."""
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
    """Ephemeral confirmation carrier bound to intent, capability, epoch and TTL.

    ``created_at`` and ``expires_at`` are wall-clock diagnostics retained for API
    compatibility. ``authority_*`` values are process-relative monotonic values
    mirrored for internal diagnostics/tests; only ``ConfirmationState`` owns and
    validates canonical authority. Mutating this object never mutates canonical
    pending authority.
    """

    token: str
    intent_hash: str
    risk_tier: RiskTier
    created_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # diagnostic wall-clock expiry only
    consumed: bool = False
    capability_name: str = ""
    confirmation_epoch: int = 0
    authority_created_at: float = 0.0
    authority_expires_at: float = 0.0

    def is_expired(self, authority_now: float) -> bool:
        """Diagnostic comparison for an explicit monotonic sample, never authority."""
        return authority_now >= self.authority_expires_at


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
    confirmation_token: Optional[ConfirmationToken] = None
    blocked_by: Optional[str] = None  # kill_switch | dedupe | token_bucket | reconciliation_required | stale_confirmation_epoch | expired_token | intent_mismatch | capability_mismatch | consumed_token

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        token = d.get("confirmation_token")
        if isinstance(token, dict):
            # Monotonic values are process-relative authority internals, not a
            # caller-facing timestamp contract. Keep epoch + wall diagnostics.
            token.pop("authority_created_at", None)
            token.pop("authority_expires_at", None)
        return d
