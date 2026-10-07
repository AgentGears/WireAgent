"""M7 Layer 5 — the bounded retained request table (frozen §10.5 / M7-RV04).

The exact frozen semantics under test:

    new id + canonical request          -> execute once
    same id + same in-flight request    -> join/await or request_in_progress
    same id + same completed request    -> return retained response
    same id + different request         -> protocol violation; no execution

In-flight entries are pinned and non-evictable; capacity pressure refuses
NEW admission rather than evicting live work (M7-T57). Completed entries
are process-local and bounded; after eviction there is no transport-level
exactly-once claim (M7-T25) — M5/M6 remains the external-effect authority.

The table KEY is the request_id; the stored comparison VALUE is the
canonical normalized request identity (which itself EXCLUDES request_id,
runtime_build_id, and authority_instance_id — the Layer-4 rule retained
by design so transport identity never masquerades as semantic equality).
"""

from __future__ import annotations

import pytest

from webwire.authority_ipc_protocol import canonical_request_identity
from webwire.authority_ipc_request_table import RetainedRequestTable


def _canonical(operation: str = "post_text", payload: dict | None = None) -> str:
    return canonical_request_identity(operation, payload or {"text": "hello"})


async def test_new_id_executes_once() -> None:
    """M7-T22 (fresh id): classify returns NEW with a completable future."""
    table = RetainedRequestTable()
    decision = table.classify("a" * 32, _canonical())
    assert decision.kind == "new"
    assert decision.future is not None and not decision.future.done()
    table.complete("a" * 32, b"RESPONSE-1")
    assert decision.future.result() == b"RESPONSE-1"
    assert table.completed_count == 1


async def test_same_id_same_inflight_joins_single_execution() -> None:
    """M7-T22: a concurrent duplicate with the SAME canonical request
    JOINs the in-flight entry — the response is shared, nothing re-runs."""
    table = RetainedRequestTable()
    key = "b" * 32
    canonical = _canonical()
    first = table.classify(key, canonical)
    assert first.kind == "new"
    duplicate = table.classify(key, canonical)
    assert duplicate.kind == "join"
    assert duplicate.future is first.future
    table.complete(key, b"ONE-EXECUTION")
    assert first.future.result() == b"ONE-EXECUTION"
    assert duplicate.future.result() == b"ONE-EXECUTION"


async def test_same_id_same_completed_returns_retained_response() -> None:
    """M7-T23: after completion, the same id + same canonical request
    returns the RETAINED response without any new execution."""
    table = RetainedRequestTable()
    key = "c" * 32
    canonical = _canonical()
    table.classify(key, canonical)
    table.complete(key, b"RETAINED")
    decision = table.classify(key, canonical)
    assert decision.kind == "retained"
    assert decision.response == b"RETAINED"


async def test_same_id_different_canonical_is_protocol_violation() -> None:
    """M7-T24: a reused id with a DIFFERENT canonical request is a
    protocol violation — no execution, whether in-flight or completed."""
    table = RetainedRequestTable()
    key = "d" * 32
    table.classify(key, _canonical(payload={"text": "one"}))
    # In-flight mismatch.
    decision = table.classify(key, _canonical(payload={"text": "two"}))
    assert decision.kind == "violation"
    table.complete(key, b"DONE")
    # Completed mismatch.
    decision = table.classify(key, _canonical(payload={"text": "two"}))
    assert decision.kind == "violation"
    assert decision.response is None


async def test_key_order_independence_one_canonical_identity() -> None:
    """M7-T58: semantically identical validated requests encoded with
    different JSON object key order produce ONE canonical identity, so
    the duplicate joins instead of violating."""
    left = canonical_request_identity("post_text", {"text": "x", "extra": {"a": 1, "b": 2}})
    right = canonical_request_identity("post_text", {"extra": {"b": 2, "a": 1}, "text": "x"})
    assert left == right
    table = RetainedRequestTable()
    key = "e" * 32
    first = table.classify(key, left)
    duplicate = table.classify(key, right)
    assert first.kind == "new"
    assert duplicate.kind == "join"


async def test_request_id_is_not_part_of_canonical_equality() -> None:
    """The frozen Layer-4 rule, retained in Layer 5: the table KEY is the
    request_id; canonical equality ignores it. Two DIFFERENT ids with the
    same canonical request are two independent entries (both NEW)."""
    table = RetainedRequestTable()
    canonical = _canonical()
    assert table.classify("1" * 32, canonical).kind == "new"
    assert table.classify("2" * 32, canonical).kind == "new"


async def test_inflight_entries_are_pinned_at_capacity() -> None:
    """M7-T57: with every in-flight slot occupied by live work, NEW
    admission is refused (backpressure) — live entries are never evicted
    and still complete normally."""
    table = RetainedRequestTable(max_inflight=2)
    held = []
    for rid in ("f" * 32, "g" * 32):
        decision = table.classify(rid, _canonical(payload={"text": rid[:1]}))
        assert decision.kind == "new"
        held.append((rid, decision))
    # Capacity full of LIVE work: the third admission is refused.
    refused = table.classify("h" * 32, _canonical(payload={"text": "t3"}))
    assert refused.kind == "full"
    # The pinned entries still complete and are retained.
    for rid, decision in held:
        table.complete(rid, b"OK-" + rid[:1].encode())
        assert decision.future.done()


async def test_completed_entries_evict_bounded_no_exactly_once_claim() -> None:
    """M7-T25: completed entries are bounded; after eviction the same id
    behaves as NEW again — eviction grants no replay authority and makes
    no exactly-once claim (the deeper M5/M6 semantics govern)."""
    table = RetainedRequestTable(max_completed=2)
    for i in range(3):
        rid = chr(ord("i") + i) * 32
        table.classify(rid, _canonical(payload={"text": f"n{i}"}))
        table.complete(rid, b"R" + str(i).encode())
    # The oldest completed entry (i=0) was evicted: it classifies as NEW.
    again = table.classify("i" * 32, _canonical(payload={"text": "n0"}))
    assert again.kind == "new"
    assert table.completed_count == 2
    # The newest retained entry is still retained.
    kept = table.classify("k" * 32, _canonical(payload={"text": "n2"}))
    assert kept.kind == "retained"


async def test_failed_response_frames_are_retained_too() -> None:
    """A completed request whose terminal outcome was an error envelope is
    still a COMPLETED request: the retry observes the same terminal frame
    instead of re-executing (response uncertainty, not re-run)."""
    table = RetainedRequestTable()
    key = "l" * 32
    table.classify(key, _canonical())
    table.complete(key, b'{"ok": false}')
    decision = table.classify(key, _canonical())
    assert decision.kind == "retained"
    assert decision.response == b'{"ok": false}'


async def test_abandon_releases_inflight_and_wakes_joiners() -> None:
    """Infrastructure failure path: an admitted entry that never produced
    a response frame is abandoned — the in-flight slot frees and joiners
    observe the failure rather than hanging forever."""
    table = RetainedRequestTable()
    key = "m" * 32
    first = table.classify(key, _canonical())
    joiner = table.classify(key, _canonical())
    assert joiner.kind == "join"
    table.abandon(key, RuntimeError("owner-side infrastructure failure"))
    assert first.future.done() and first.future.exception() is not None
    with pytest.raises(RuntimeError):
        await joiner.future
    # The slot is free: the same id classifies as NEW again.
    assert table.classify(key, _canonical()).kind == "new"


async def test_table_is_process_local_to_its_instance() -> None:
    """M7-T26 (process-local half): a NEW table (a new owner instance)
    starts empty — old ids carry over nothing."""
    first = RetainedRequestTable()
    first.classify("n" * 32, _canonical())
    first.complete("n" * 32, b"OLD")
    successor = RetainedRequestTable()
    assert successor.classify("n" * 32, _canonical()).kind == "new"
    assert successor.completed_count == 0
