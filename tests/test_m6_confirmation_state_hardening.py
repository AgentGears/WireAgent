"""Adversarial M6 Layer-3 confirmation-state authority tests."""

from __future__ import annotations

import math

import pytest

from webwire.safety import ConfirmationState, RiskTier


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


@pytest.mark.parametrize("ttl", [0.0, -1.0, math.inf, -math.inf, math.nan])
def test_nonfinite_or_nonpositive_ttl_is_rejected(ttl: float) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        ConfirmationState(ttl_seconds=ttl)


@pytest.mark.parametrize("sample", [math.inf, -math.inf, math.nan])
def test_nonfinite_authority_clock_fails_closed(sample: float) -> None:
    state = ConfirmationState(
        monotonic_clock=_Clock(sample),
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token",
    )
    with pytest.raises(RuntimeError, match="non-finite"):
        state.issue(
            intent_hash="intent",
            risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
            capability_name="like",
        )


def test_regressing_authority_clock_fails_closed() -> None:
    mono = _Clock(10.0)
    state = ConfirmationState(
        monotonic_clock=mono,
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token",
    )
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    mono.value = 9.0

    with pytest.raises(RuntimeError, match="regressed"):
        state.validate_and_consume(
            token.token,
            intent_hash="intent",
            risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
            capability_name="like",
        )


def test_mutating_returned_token_cannot_resurrect_stale_epoch() -> None:
    mono = _Clock(10.0)
    state = ConfirmationState(
        monotonic_clock=mono,
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token",
    )
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    original_key = token.token
    state.advance_epoch()

    # Forge every mutable carrier field that would matter if the state trusted
    # the public token object. Canonical private authority must still win.
    token.confirmation_epoch = state.current_epoch
    token.intent_hash = "forged"
    token.capability_name = "post_text"
    token.authority_expires_at = 1_000_000.0
    token.consumed = False

    _, blocked = state.validate_and_consume(
        original_key,
        intent_hash="forged",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="post_text",
    )
    assert blocked == "stale_confirmation_epoch"


def test_mutating_token_deadline_cannot_extend_private_authority() -> None:
    mono = _Clock(10.0)
    state = ConfirmationState(
        ttl_seconds=5.0,
        monotonic_clock=mono,
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token",
    )
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    token.authority_expires_at = 1_000_000.0
    mono.value = 16.0

    _, blocked = state.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    assert blocked == "expired_token"


def test_mutating_consumed_mirror_cannot_restore_single_use_authority() -> None:
    state = ConfirmationState(
        monotonic_clock=_Clock(10.0),
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token",
    )
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    _, blocked = state.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    assert blocked is None

    token.consumed = False
    _, blocked = state.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    assert blocked == "consumed_token"


def test_token_issued_after_epoch_advance_uses_new_epoch_and_can_consume() -> None:
    state = ConfirmationState(
        monotonic_clock=_Clock(10.0),
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "new-token",
    )
    assert state.advance_epoch() == 1
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )

    assert token.confirmation_epoch == 1
    _, blocked = state.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    assert blocked is None
