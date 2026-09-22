"""Risk registry — maps action types to RiskMeta + CompensationMeta.

Per review Q4: risk is multi-dimensional metadata, not a flat tier. The registry
holds the metadata; RiskMeta.derive_tier() produces the tier. Four tiers per
the review:
  PRIVATE_REVERSIBLE               — bookmark
  PUBLIC_REVERSIBLE_ENGAGEMENT     — like, follow
  PUBLIC_AMPLIFYING_REVERSIBLE     — repost/retweet
  PUBLIC_CONTENT_IRREVERSIBLE      — post, reply, quote
"""

from __future__ import annotations

from typing import Optional

from webwire.safety.models import (
    Amplification,
    CompensationMeta,
    Reversibility,
    RiskMeta,
    RiskTier,
    Visibility,
)

__all__ = ["RiskRegistry", "DEFAULT_REGISTRY"]


class RiskRegistry:
    """Maps action_type -> (RiskMeta, CompensationMeta)."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[RiskMeta, CompensationMeta]] = {}

    def register(
        self,
        action_type: str,
        risk_meta: RiskMeta,
        compensation: CompensationMeta,
    ) -> None:
        self._entries[action_type] = (risk_meta, compensation)

    def get(self, action_type: str) -> Optional[tuple[RiskMeta, CompensationMeta]]:
        return self._entries.get(action_type)

    def get_meta(self, action_type: str) -> Optional[RiskMeta]:
        e = self._entries.get(action_type)
        return e[0] if e else None

    def get_compensation(self, action_type: str) -> Optional[CompensationMeta]:
        e = self._entries.get(action_type)
        return e[1] if e else None

    def known_actions(self) -> list[str]:
        return sorted(self._entries.keys())


def _build_default() -> RiskRegistry:
    """Build the default registry per the review's 4-tier model.
    All action types are registered now (including Phase 4 ones) so the safety
    posture is complete before any write capability exists."""
    reg = RiskRegistry()

    # --- Phase 3: reversible ---
    # bookmark: private, reversible, no amplification.
    reg.register(
        "bookmark",
        RiskMeta(
            visibility=Visibility.PRIVATE,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.NONE,
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="remove_bookmark",
        ),
    )
    reg.register(
        "remove_bookmark",
        RiskMeta(
            visibility=Visibility.PRIVATE,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.NONE,
        ),
        CompensationMeta(supports_compensation=False),
    )
    # like: public engagement signal, reversible.
    reg.register(
        "like",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.ENGAGEMENT_SIGNAL,
            residual_side_effects=("may_notify_author", "may_train_recommendations"),
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="unlike",
            residual_side_effects=("notification_already_sent",),
        ),
    )
    reg.register(
        "unlike",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.ENGAGEMENT_SIGNAL,
        ),
        CompensationMeta(supports_compensation=False),
    )

    # --- Phase 4: public engagement ---
    reg.register(
        "follow",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.ENGAGEMENT_SIGNAL,
        ),
        CompensationMeta(supports_compensation=True, compensation_action="unfollow"),
    )
    reg.register(
        "unfollow",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.ENGAGEMENT_SIGNAL,
        ),
        CompensationMeta(supports_compensation=False),
    )

    # --- Phase 4: amplifying ---
    reg.register(
        "repost",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.BROADCAST,
            residual_side_effects=("may_notify_author", "visible_in_feeds"),
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="unrepost",
            residual_side_effects=("may_have_been_seen",),
        ),
    )
    reg.register(
        "unrepost",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.REVERSIBLE,
            amplification=Amplification.BROADCAST,
        ),
        CompensationMeta(supports_compensation=False),
    )

    # --- Phase 4: public content (irreversible in the true sense) ---
    reg.register(
        "post",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.COMPENSATABLE,  # can delete, but...
            amplification=Amplification.BROADCAST,
            content_creation=True,
            residual_side_effects=("screenshots", "notifications", "feed_caches", "recipients"),
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="delete_post",
            residual_side_effects=("deletion_is_not_undo", "recipients_may_have_seen"),
        ),
    )
    reg.register(
        "reply",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.COMPENSATABLE,
            amplification=Amplification.BROADCAST,
            content_creation=True,
            residual_side_effects=("notifications", "thread_context"),
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="delete_reply",
            residual_side_effects=("deletion_is_not_undo",),
        ),
    )
    reg.register(
        "quote",
        RiskMeta(
            visibility=Visibility.PUBLIC,
            reversibility=Reversibility.COMPENSATABLE,
            amplification=Amplification.BROADCAST,
            content_creation=True,
            residual_side_effects=("screenshots", "notifications", "amplifies_quoted_post"),
        ),
        CompensationMeta(
            supports_compensation=True,
            compensation_action="delete_quote",
            residual_side_effects=("deletion_is_not_undo",),
        ),
    )
    # --- delete_post (2026-09-23): the compensation made real ---
    # Deleting own content is IRREVERSIBLE (restoration is impossible; repost
    # creates a new post) but de-amplifying and non-creative — it fails every
    # specific derive_tier branch and lands, correctly, in the conservative
    # PUBLIC_CONTENT_IRREVERSIBLE default. It cannot itself be compensated:
    # recreation is a new post; thread replies/quotes break; copies persist.
    reg.register(
        "delete_post",
        RiskMeta(
            visibility=Visibility.PUBLIC,          # the effect is publicly visible
            reversibility=Reversibility.IRREVERSIBLE,
            amplification=Amplification.NONE,       # it de-amplifies
            residual_side_effects=(
                "deletion_breaks_threads_and_quotes",
                "copies_screenshots_and_caches_may_persist",
            ),
        ),
        CompensationMeta(
            supports_compensation=False,
            residual_side_effects=(
                "deletion_is_not_undo",
                "recreation_is_a_new_post",
            ),
        ),
    )

    return reg


DEFAULT_REGISTRY = _build_default()
