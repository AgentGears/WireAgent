"""Regression locks for read-only M5 execution-model fields."""

from __future__ import annotations

import pytest

from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    EffectAttempt,
    GrantState,
    GrantStateError,
)


def _grant():  # type: ignore[no-untyped-def]
    store = ApprovalGrantStore(clock=lambda: 100.0)
    return store.mint(
        intent_hash="i" * 32,
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="123",
        policy_binding="p" * 64,
        authorization_epoch=7,
    )


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("intent_hash", "changed"),
        ("actor_id", "@other"),
        ("action_type", "reply"),
        ("target_type", "user"),
        ("target_id", "999"),
        ("policy_binding", "changed-policy"),
        ("authorization_epoch", 8),
        ("grant_id", "changed-grant"),
        ("issued_at", 0.0),
        ("expires_at", 999999.0),
        ("max_precommit_attempts", 999),
        ("state", GrantState.SPENT),
        ("claimed_by", "forged-attempt"),
        ("precommit_attempts", 0),
        ("clock", lambda: 0.0),
    ],
)
def test_approval_fields_are_read_only_after_issue(
    field_name: str,
    replacement: object,
) -> None:
    grant = _grant()
    with pytest.raises(GrantStateError, match="read-only"):
        setattr(grant, field_name, replacement)


def test_approval_lifecycle_methods_still_change_internal_state() -> None:
    grant = _grant()
    attempt = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        attempt.attempt_id,
        intent_hash=grant.intent_hash,
        actor_id=grant.actor_id,
        policy_binding=grant.policy_binding,
        authorization_epoch=grant.authorization_epoch,
    )
    assert grant.claimed_by == attempt.attempt_id

    attempt.mark_no_effect(grant)
    assert grant.claimed_by is None
    assert grant.precommit_attempts == 1
    assert grant.state is GrantState.ACTIVE

    retry = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        retry.attempt_id,
        intent_hash=grant.intent_hash,
        actor_id=grant.actor_id,
        policy_binding=grant.policy_binding,
        authorization_epoch=grant.authorization_epoch,
    )
    grant.spend()
    assert grant.state is GrantState.SPENT


@pytest.mark.parametrize(
    ("field_name", "replacement"),
    [
        ("grant_id", "changed-grant"),
        ("attempt_id", "changed-attempt"),
        ("effect_id", "changed-effect"),
        ("state", AttemptState.EFFECT_CONFIRMED),
        ("reservation_started", True),
    ],
)
def test_attempt_fields_are_read_only_after_creation(
    field_name: str,
    replacement: object,
) -> None:
    attempt = EffectAttempt(grant_id="grant")
    with pytest.raises(GrantStateError, match="read-only"):
        setattr(attempt, field_name, replacement)


def test_attempt_lifecycle_methods_still_change_internal_state() -> None:
    grant = _grant()
    attempt = EffectAttempt(grant_id=grant.grant_id)
    grant.claim(
        attempt.attempt_id,
        intent_hash=grant.intent_hash,
        actor_id=grant.actor_id,
        policy_binding=grant.policy_binding,
        authorization_epoch=grant.authorization_epoch,
    )
    attempt.begin_reservation(grant)
    assert attempt.reservation_started is True
    attempt.mark_reserved(grant)
    assert attempt.state is AttemptState.RESERVED
    grant.spend()
    attempt.mark_effect_unknown()
    assert attempt.state is AttemptState.EFFECT_UNKNOWN
