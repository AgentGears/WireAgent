"""M5 layer-1 tests for EffectPolicy derivation and registry truth."""

from __future__ import annotations

import pytest

from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
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


def test_default_policy_table_matches_frozen_m5_assignments() -> None:
    assert DEFAULT_EFFECT_POLICIES.require("post").durability == DurabilityPolicy.REQUIRED
    assert DEFAULT_EFFECT_POLICIES.require("reply").durability == DurabilityPolicy.REQUIRED
    assert DEFAULT_EFFECT_POLICIES.require("quote").durability == DurabilityPolicy.REQUIRED

    delete = DEFAULT_EFFECT_POLICIES.require("delete_post")
    assert delete.replay_semantics == ReplaySemantics.SAFE_TARGET_DELETE
    assert delete.durability == DurabilityPolicy.REQUIRED

    assert DEFAULT_EFFECT_POLICIES.require("like").durability == DurabilityPolicy.BEST_EFFORT
    assert DEFAULT_EFFECT_POLICIES.require("bookmark").durability == DurabilityPolicy.BEST_EFFORT


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
    assert p1.binding_hash() == p2.binding_hash()
    assert p1.binding_hash() != broader.binding_hash()
