"""Regressions for bounded Layer-5 approval-grant retention."""

from __future__ import annotations

from webwire.safety.m5_execution_runtime import _PruningApprovalGrantStore


def _mint(store: _PruningApprovalGrantStore, suffix: str):  # type: ignore[no-untyped-def]
    return store.mint(
        intent_hash=f"intent-{suffix}",
        actor_id="actor",
        action_type="like",
        target_type="post",
        target_id=f"post-{suffix}",
        policy_binding="policy",
        authorization_epoch=0,
    )


def test_next_mint_prunes_terminal_grants_but_retains_live_active_grants() -> None:
    store = _PruningApprovalGrantStore(ttl_seconds=300.0)
    active = _mint(store, "active")
    spent = _mint(store, "spent")
    revoked = _mint(store, "revoked")
    spent.spend()
    revoked.revoke()

    current = _mint(store, "current")

    assert store.get(active.grant_id) is active
    assert store.get(current.grant_id) is current
    assert store.get(spent.grant_id) is None
    assert store.get(revoked.grant_id) is None
    assert len(store) == 2


def test_next_mint_prunes_elapsed_active_grants() -> None:
    now = [10.0]
    store = _PruningApprovalGrantStore(
        clock=lambda: now[0],
        ttl_seconds=5.0,
    )
    expired = _mint(store, "expired")
    now[0] = 16.0

    current = _mint(store, "current")

    assert store.get(expired.grant_id) is None
    assert store.get(current.grant_id) is current
    assert len(store) == 1


def test_repeated_terminal_writes_do_not_accumulate_in_default_runtime_store() -> None:
    store = _PruningApprovalGrantStore(ttl_seconds=300.0)

    for index in range(25):
        grant = _mint(store, str(index))
        grant.spend()

    # The last terminal grant remains observable until the next issue, preserving
    # immediate get() semantics without retaining the entire historical sequence.
    assert len(store) == 1

    current = _mint(store, "live")
    assert store.get(current.grant_id) is current
    assert len(store) == 1
