"""M5 layer 2 — ApprovalGrant / EffectAttempt lifecycle tests.

Acceptance target (per the layer's mandate): the lifecycle itself, with
T13 and T14 runnable end-to-end at model level:

T13 — clean precommit failure preserves approval:
    claim → PROVEN no effect → claim released → grant ACTIVE → a second
    attempt may claim the same approval.

T14 — reservation consumes approval:
    claim → RESERVED → (gateway spends after the durable append, simulated
    here as the two documented calls) → the same approval cannot be
    reclaimed; a new claim attempt is denied on a SPENT grant.

Plus the T10/T11 prerequisites (actor / intent binding denials), the
epoch rule (spec 7), expiry, the attempt budget (spec 5.3), and every
illegal transition.
"""

from __future__ import annotations

import pytest

from webwire.safety.execution_models import (
    ApprovalGrantStore,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantClaimDenied,
    GrantState,
    GrantStateError,
)


# A fixed clock: grants minted at t=100, TTL 300 → expire at t=400.
def _clock(t: list[float]) -> "callable[[], float]":
    return lambda: t[0]


@pytest.fixture
def store() -> ApprovalGrantStore:
    return ApprovalGrantStore(clock=lambda: 100.0, ttl_seconds=300.0)


def _mint(store: ApprovalGrantStore, epoch: int = 0) -> str:
    g = store.mint(
        intent_hash="a" * 32,
        actor_id="infaag",
        action_type="post",
        target_type="none",
        target_id="none",
        policy_binding="b" * 64,
        authorization_epoch=epoch,
    )
    return g.grant_id


def _claim_args(**overrides) -> dict:
    base = dict(
        intent_hash="a" * 32,
        actor_id="infaag",
        authorization_epoch=0,
    )
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# T13 — clean precommit failure preserves approval
# ---------------------------------------------------------------------------

def test_T13_clean_precommit_failure_preserves_approval(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)

    a1 = EffectAttempt(grant_id=grant_id)
    grant.claim(a1.attempt_id, **_claim_args())

    # Preparation fails with PROVEN no external effect.
    a1.mark_no_effect(grant)

    assert a1.state is AttemptState.NO_EFFECT
    assert grant.state is GrantState.ACTIVE, "clean failure must not consume approval"
    assert grant.claimed_by is None, "claim released"
    assert grant.precommit_attempts == 1

    # The same approval funds another attempt.
    a2 = EffectAttempt(grant_id=grant_id)
    grant.claim(a2.attempt_id, **_claim_args())
    assert grant.claimed_by == a2.attempt_id


def test_attempt_budget_exhausts_but_grant_stays_active(store: ApprovalGrantStore) -> None:
    """Spec 5.3: three clean failures stop automated execution without
    consuming the approval — a human decides what happens next."""
    grant_id = _mint(store)
    grant = store.get(grant_id)

    for _ in range(3):
        a = EffectAttempt(grant_id=grant_id)
        grant.claim(a.attempt_id, **_claim_args())
        a.mark_no_effect(grant)

    assert grant.state is GrantState.ACTIVE
    a4 = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a4.attempt_id, **_claim_args())
    assert exc.value.reason == "attempts_exhausted"


# ---------------------------------------------------------------------------
# T14 — reservation consumes approval
# ---------------------------------------------------------------------------

def test_T14_reservation_consumes_approval(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)

    attempt = EffectAttempt(grant_id=grant_id)
    grant.claim(attempt.attempt_id, **_claim_args())

    # The gateway's documented two-step, composed here (spec 6.1): the
    # durable append happens between these calls in layer 3.
    attempt.mark_reserved(grant)
    grant.spend()

    assert attempt.state is AttemptState.RESERVED
    assert grant.state is GrantState.SPENT

    # The same approval cannot be reclaimed — by the crash-restarted caller
    # or anyone else. (The RecoveryGuard's matching-intent deny is layer 6;
    # the model-level prerequisite is that no claim can ever succeed again.)
    retry = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(retry.attempt_id, **_claim_args())
    assert exc.value.reason == "spent"


def test_spent_grant_is_irrevocably_terminal(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    grant.spend()
    with pytest.raises(GrantStateError):
        grant.spend()
    with pytest.raises(GrantStateError):
        grant.revoke()


def test_mark_reserved_does_not_spend_by_itself(store: ApprovalGrantStore) -> None:
    """Ordering contract: the attempt reaching RESERVED does NOT spend the
    grant — the gateway does, after the fsync. (A crash between the two is
    exactly the window where the ledger fact must dominate; the grant dying
    with the process is the other half of that rule.)"""
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a = EffectAttempt(grant_id=grant_id)
    grant.claim(a.attempt_id, **_claim_args())
    a.mark_reserved(grant)
    assert grant.state is GrantState.ACTIVE
    # The claim is still held — the gateway completes under it.
    assert grant.claimed_by == a.attempt_id


# ---------------------------------------------------------------------------
# Binding prerequisites (T10 / T11)
# ---------------------------------------------------------------------------

def test_intent_mismatch_denies_claim(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a.attempt_id, **_claim_args(intent_hash="f" * 32))
    assert exc.value.reason == "intent_mismatch"
    assert grant.claimed_by is None, "denied claim leaves the grant untouched"


def test_actor_mismatch_denies_claim(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a.attempt_id, **_claim_args(actor_id="someone_else"))
    assert exc.value.reason == "actor_mismatch"


def test_second_attempt_cannot_steal_the_claim(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a1 = EffectAttempt(grant_id=grant_id)
    a2 = EffectAttempt(grant_id=grant_id)
    grant.claim(a1.attempt_id, **_claim_args())
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a2.attempt_id, **_claim_args())
    assert exc.value.reason == "approval_already_claimed"
    assert grant.claimed_by == a1.attempt_id


def test_only_the_owner_releases_the_claim(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a1 = EffectAttempt(grant_id=grant_id)
    a2 = EffectAttempt(grant_id=grant_id)
    grant.claim(a1.attempt_id, **_claim_args())
    with pytest.raises(GrantStateError):
        a2.mark_no_effect(grant)
    assert grant.claimed_by == a1.attempt_id
    assert a2.state is AttemptState.PREPARING


# ---------------------------------------------------------------------------
# Epoch (spec 7)
# ---------------------------------------------------------------------------

def test_epoch_bump_revokes_outstanding_grants(store: ApprovalGrantStore) -> None:
    epoch = AuthorizationEpoch(initial=0)
    grant_id = _mint(store, epoch=epoch.current)
    grant = store.get(grant_id)

    epoch.bump()  # kill switch trip

    a = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a.attempt_id, **_claim_args(authorization_epoch=epoch.current))
    assert exc.value.reason == "epoch_mismatch"
    assert grant.state is GrantState.REVOKED, "epoch mismatch revokes, lazily"


def test_stale_epoch_cannot_validate(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store, epoch=5)
    grant = store.get(grant_id)
    with pytest.raises(GrantClaimDenied):
        grant.validate_live(
            intent_hash="a" * 32, actor_id="infaag", authorization_epoch=4
        )


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------

def test_expiry_denies_claim_and_lazily_demotes() -> None:
    t = [100.0]
    store = ApprovalGrantStore(clock=lambda: t[0], ttl_seconds=300.0)
    grant_id = _mint(store)
    grant = store.get(grant_id)

    t[0] = 500.0  # past expires_at=400
    a = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a.attempt_id, **_claim_args())
    assert exc.value.reason == "expired"
    assert grant.state is GrantState.EXPIRED


def test_not_yet_expired_claim_still_works() -> None:
    t = [100.0]
    store = ApprovalGrantStore(clock=lambda: t[0], ttl_seconds=300.0)
    grant_id = _mint(store)
    grant = store.get(grant_id)

    t[0] = 399.9
    a = EffectAttempt(grant_id=grant_id)
    grant.claim(a.attempt_id, **_claim_args())  # boundary: still live
    assert grant.claimed_by == a.attempt_id


def test_operator_revoke_is_terminal(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    grant.revoke()
    a = EffectAttempt(grant_id=grant_id)
    with pytest.raises(GrantClaimDenied) as exc:
        grant.claim(a.attempt_id, **_claim_args())
    assert exc.value.reason == "revoked"


# ---------------------------------------------------------------------------
# Attempt state machine strictness
# ---------------------------------------------------------------------------

def test_attempt_transitions_enforced(store: ApprovalGrantStore) -> None:
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a = EffectAttempt(grant_id=grant_id)
    grant.claim(a.attempt_id, **_claim_args())

    a.mark_reserved(grant)
    with pytest.raises(GrantStateError):
        a.mark_no_effect(grant), "RESERVED cannot go to NO_EFFECT"
    with pytest.raises(GrantStateError):
        a.mark_reserved(grant)

    a.mark_effect_confirmed()
    assert a.state is AttemptState.EFFECT_CONFIRMED
    with pytest.raises(GrantStateError):
        a.mark_effect_unknown(), "terminal outcome cannot change"


def test_unfenced_attempt_may_skip_reserved(store: ApprovalGrantStore) -> None:
    """Spec 5.2: non-fenced effects have no RESERVED step; PREPARING goes
    straight to an outcome. Ordering per spec 6: the unfenced spend is the
    process-local CAS at the commit boundary — BEFORE the outcome, under the
    claim. One grant funds one boundary crossing, so the two attempts here
    draw on two grants."""
    # Unknown outcome on a best-effort effect.
    grant_a = store.get(_mint(store))
    a = EffectAttempt(grant_id=grant_a.grant_id)
    grant_a.claim(a.attempt_id, **_claim_args())
    grant_a.spend()  # unfenced: CAS at the boundary
    a.mark_effect_unknown()
    assert a.state is AttemptState.EFFECT_UNKNOWN
    assert grant_a.state is GrantState.SPENT

    # Confirmed outcome on another grant.
    grant_b = store.get(_mint(store))
    b = EffectAttempt(grant_id=grant_b.grant_id)
    grant_b.claim(b.attempt_id, **_claim_args())
    grant_b.spend()
    b.mark_effect_confirmed()
    assert b.state is AttemptState.EFFECT_CONFIRMED
    assert grant_b.state is GrantState.SPENT


def test_outcome_states_do_not_release_or_spend(store: ApprovalGrantStore) -> None:
    """Recording an outcome changes only the attempt. Grant transitions stay
    explicit gateway operations — the models never blur that line."""
    grant_id = _mint(store)
    grant = store.get(grant_id)
    a = EffectAttempt(grant_id=grant_id)
    grant.claim(a.attempt_id, **_claim_args())
    a.mark_effect_unknown()
    assert grant.state is GrantState.ACTIVE, "outcomes do not spend; the gateway does"


# ---------------------------------------------------------------------------
# Store / ephemerality
# ---------------------------------------------------------------------------

def test_store_mints_and_gets() -> None:
    store = ApprovalGrantStore()
    grant_id = _mint(store)
    assert store.get(grant_id) is not None
    assert len(store) == 1
    assert store.get("missing") is None


def test_grants_are_ephemeral_by_design(store: ApprovalGrantStore) -> None:
    """No persistence surface exists on the store — the ledger owns durable
    safety state. This locks the design decision against accidental
    regression (someone 'helpfully' adding save/load later)."""
    import inspect

    from webwire.safety.execution_models import ApprovalGrantStore as S

    src = inspect.getsource(S)
    assert "save" not in src and "load" not in src and "persist" not in src
