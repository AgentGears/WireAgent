"""M5 tests for EffectPolicy derivation and registry truth."""

from __future__ import annotations

import pytest

from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
    PreparationVerb,
    ReplaySemantics,
    derive_durability,
)
from webwire.safety.models import RiskTier
from webwire.safety.risk_registry import DEFAULT_REGISTRY


def test_unknown_replay_is_conservatively_fenced() -> None:
    assert (
        derive_durability(
            RiskTier.PRIVATE_REVERSIBLE,
            ReplaySemantics.UNKNOWN,
        )
        == DurabilityPolicy.REQUIRED
    )


def test_non_idempotent_create_is_fenced_even_if_risk_is_low() -> None:
    assert (
        derive_durability(
            RiskTier.PRIVATE_REVERSIBLE,
            ReplaySemantics.NON_IDEMPOTENT_CREATE,
        )
        == DurabilityPolicy.REQUIRED
    )


def test_consequential_delete_stays_fenced_despite_safe_replay() -> None:
    assert (
        derive_durability(
            RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
            ReplaySemantics.SAFE_TARGET_DELETE,
        )
        == DurabilityPolicy.REQUIRED
    )


def test_safe_state_set_can_be_best_effort_below_high_risk_tiers() -> None:
    assert (
        derive_durability(
            RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
            ReplaySemantics.SAFE_STATE_SET,
        )
        == DurabilityPolicy.BEST_EFFORT
    )


def test_registry_rejects_durability_downgrade() -> None:
    reg = EffectPolicyRegistry()
    bad = EffectPolicy(
        action_type="bad_create",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        allowed_effects=frozenset({EffectVerb.SUBMIT_CONTENT}),
        replay_semantics=ReplaySemantics.NON_IDEMPOTENT_CREATE,
        durability=DurabilityPolicy.BEST_EFFORT,
    )
    with pytest.raises(ValueError, match="does not match derived requirement"):
        reg.register(bad)


def test_default_policy_table_matches_m5_assignments() -> None:
    content_preparation = frozenset(
        {
            PreparationVerb.OPEN_COMPOSER,
            PreparationVerb.FILL_COMPOSER,
            PreparationVerb.ATTACH_MEDIA,
        }
    )
    for action in ("post", "reply", "quote"):
        content = DEFAULT_EFFECT_POLICIES.require(action)
        assert content.durability == DurabilityPolicy.REQUIRED
        assert content.preparation_effects == content_preparation
        assert content.allowed_effects == frozenset({EffectVerb.SUBMIT_CONTENT})

    delete = DEFAULT_EFFECT_POLICIES.require("delete_post")
    assert delete.replay_semantics == ReplaySemantics.SAFE_TARGET_DELETE
    assert delete.durability == DurabilityPolicy.REQUIRED
    assert delete.preparation_effects == frozenset()
    assert delete.allowed_effects == frozenset({EffectVerb.DELETE_POST})

    bookmark = DEFAULT_EFFECT_POLICIES.require("bookmark")
    assert bookmark.replay_semantics is ReplaySemantics.SAFE_STATE_SET
    assert bookmark.durability is DurabilityPolicy.BEST_EFFORT
    assert bookmark.preparation_effects == frozenset()
    assert bookmark.allowed_effects == frozenset({EffectVerb.SET_BOOKMARK})

    # Directional/state-first DOM behavior is necessary but not sufficient to
    # prove replay safety for public engagement. A repeated like/unlike may have
    # residual platform effects even when the final boolean state is unchanged.
    for action, effect in (
        ("like", EffectVerb.SET_LIKE),
        ("unlike", EffectVerb.CLEAR_LIKE),
    ):
        engagement = DEFAULT_EFFECT_POLICIES.require(action)
        assert engagement.replay_semantics is ReplaySemantics.UNKNOWN
        assert engagement.durability is DurabilityPolicy.REQUIRED
        assert engagement.preparation_effects == frozenset()
        assert engagement.allowed_effects == frozenset({effect})

    for action in ("follow", "unfollow", "repost", "unrepost"):
        future = DEFAULT_EFFECT_POLICIES.require(action)
        assert future.replay_semantics is ReplaySemantics.UNKNOWN
        assert future.durability is DurabilityPolicy.REQUIRED


def test_public_engagement_dom_idempotence_does_not_imply_best_effort() -> None:
    """Lock the residual-effects distinction into the default policy truth."""
    for action in ("like", "unlike"):
        policy = DEFAULT_EFFECT_POLICIES.require(action)
        assert policy.replay_semantics is ReplaySemantics.UNKNOWN
        assert policy.durability is DurabilityPolicy.REQUIRED


def test_every_existing_risk_action_has_an_effect_policy() -> None:
    assert set(DEFAULT_EFFECT_POLICIES.known_actions()) == set(DEFAULT_REGISTRY.known_actions())


def test_policy_binding_is_stable_and_sensitive_to_authority() -> None:
    p1 = EffectPolicy.derive(
        action_type="x",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        allowed_effects={EffectVerb.SET_BOOKMARK},
        replay_semantics=ReplaySemantics.SAFE_STATE_SET,
    )
    p2 = EffectPolicy.derive(
        action_type="x",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        allowed_effects={EffectVerb.SET_BOOKMARK},
        replay_semantics=ReplaySemantics.SAFE_STATE_SET,
    )
    broader = EffectPolicy.derive(
        action_type="x",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        allowed_effects={EffectVerb.SET_BOOKMARK, EffectVerb.CLEAR_BOOKMARK},
        replay_semantics=ReplaySemantics.SAFE_STATE_SET,
    )
    prep_changed = EffectPolicy.derive(
        action_type="x",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        allowed_effects={EffectVerb.SET_BOOKMARK},
        replay_semantics=ReplaySemantics.SAFE_STATE_SET,
        preparation_effects={PreparationVerb.OPEN_COMPOSER},
    )
    assert p1.binding_hash() == p2.binding_hash()
    assert p1.binding_hash() != broader.binding_hash()
    assert p1.binding_hash() != prep_changed.binding_hash()


def test_no_per_action_uncertainty_policy_exists() -> None:
    """Uncertainty behavior is not an action-author controlled escape hatch.

    Explicit EFFECT_UNKNOWN is globally reconciliation-required. A crash with
    no BEST_EFFORT durable fact may be replayed only because replay semantics
    are registry-controlled and implementation-tested; no separate
    ``uncertainty_policy`` can weaken that contract.
    """
    from pathlib import Path as _Path

    import webwire.safety.effect_policy as ep

    assert not hasattr(ep, "UncertaintyPolicy")
    assert not hasattr(ep.EffectPolicy, "uncertainty_policy")
    src = _Path(ep.__file__).read_text(encoding="utf-8")
    assert "uncertainty_policy" not in src
