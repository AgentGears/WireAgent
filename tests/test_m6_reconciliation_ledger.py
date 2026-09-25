"""M6 Layer-1 record, schema, history, and serialization regressions."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.reconciliation_ledger import (
    ReconciliationLedger,
    ReconciliationLedgerCorruptError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)


def _evidence() -> dict:
    return {
        "basis": "operator-inspection",
        "observed_at": "2026-09-26T00:00:00+00:00",
        "observations": [
            {
                "kind": "remote-object",
                "value": "present",
                "details": {"stable": True, "count": 1},
            }
        ],
    }


def _record(
    *,
    reconciliation_id: str = "rec-1",
    effect_id: str = "fx-1",
    verdict: ReconciliationVerdict = ReconciliationVerdict.CONFIRMED_EFFECT,
    evidence: dict | None = None,
    timestamp: str = "2026-09-26T00:01:00+00:00",
) -> ReconciliationRecord:
    proof = evidence if evidence is not None else _evidence()
    return ReconciliationRecord(
        reconciliation_id=reconciliation_id,
        effect_id=effect_id,
        semantic_key="@actor|post|post|123|semantic",
        action_type="post",
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="@actor",
        target_type="post",
        target_id="123",
        verdict=verdict,
        operator_id="operator-local",
        evidence_hash=canonical_evidence_hash(proof),
        evidence=proof,
        timestamp=timestamp,
    )


def test_config_exposes_reconciliation_safety_path(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    assert cfg.reconciliations_path() == tmp_path / "reconciliations.ndjson"


def test_record_round_trip_preserves_canonical_fact() -> None:
    original = _record()
    parsed = ReconciliationRecord.from_dict(json.loads(original.to_jsonl()))

    assert parsed == original
    assert parsed.evidence == original.evidence
    assert parsed.verdict is ReconciliationVerdict.CONFIRMED_EFFECT


def test_record_evidence_is_deeply_detached_from_caller_mutation() -> None:
    evidence = _evidence()
    record = _record(evidence=evidence)
    expected_hash = record.evidence_hash

    evidence["basis"] = "mutated"
    evidence["observations"][0]["details"]["stable"] = False
    detached = record.evidence
    detached["basis"] = "also-mutated"

    assert record.evidence["basis"] == "operator-inspection"
    assert record.evidence["observations"][0]["details"]["stable"] is True
    assert record.evidence_hash == expected_hash
    assert canonical_evidence_hash(record.evidence) == expected_hash


@pytest.mark.parametrize(
    "bad",
    [None, "", [], {"basis": "x"}],
)
def test_evidence_requires_nonempty_structured_minimum(bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        canonical_evidence_hash(bad)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "evidence",
    [
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00+00:00",
            "observations": [],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00+00:00",
            "observations": ["not-structured"],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00",
            "observations": [{"kind": "x"}],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T03:00:00+03:00",
            "observations": [{"kind": "x"}],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00+00:00",
            "observations": [{"bad": float("nan")}],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00+00:00",
            "observations": [{"bad": object()}],
        },
        {
            "basis": "x",
            "observed_at": "2026-09-26T00:00:00+00:00",
            "observations": [{1: "numeric-key"}],
        },
    ],
)
def test_evidence_rejects_weak_or_nonportable_shapes(evidence: dict) -> None:
    with pytest.raises(ValueError):
        canonical_evidence_hash(evidence)


def test_evidence_allows_nested_finite_json() -> None:
    evidence = _evidence()
    evidence["observations"].append(
        {
            "kind": "metrics",
            "values": [None, False, 2, 0.5, {"nested": "ok"}],
        }
    )
    digest = canonical_evidence_hash(evidence)
    assert len(digest) == 64
    assert math.isfinite(evidence["observations"][1]["values"][3])


def test_hash_is_canonical_across_object_key_order() -> None:
    first = _evidence()
    second = {
        "observations": first["observations"],
        "observed_at": first["observed_at"],
        "basis": first["basis"],
    }
    assert canonical_evidence_hash(first) == canonical_evidence_hash(second)


def test_record_rejects_hash_mismatch_and_nonlowercase_digest() -> None:
    evidence = _evidence()
    kwargs = dict(
        reconciliation_id="rec",
        effect_id="fx",
        semantic_key="key",
        action_type="post",
        intent_hash="intent",
        policy_binding="policy",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        operator_id="operator",
        evidence=evidence,
    )
    with pytest.raises(ValueError, match="does not match"):
        ReconciliationRecord(evidence_hash="0" * 64, **kwargs)
    with pytest.raises(ValueError, match="lowercase"):
        ReconciliationRecord(evidence_hash="A" * 64, **kwargs)


def test_direct_record_requires_typed_verdict() -> None:
    evidence = _evidence()
    with pytest.raises(ValueError, match="ReconciliationVerdict"):
        ReconciliationRecord(
            reconciliation_id="rec",
            effect_id="fx",
            semantic_key="key",
            action_type="post",
            intent_hash="intent",
            policy_binding="policy",
            verdict="CONFIRMED_EFFECT",  # type: ignore[arg-type]
            operator_id="operator",
            evidence_hash=canonical_evidence_hash(evidence),
            evidence=evidence,
        )


@pytest.mark.parametrize(
    "timestamp",
    ["", "2026-09-26T00:00:00", "2026-09-26T03:00:00+03:00", "not-time"],
)
def test_record_timestamp_must_be_explicit_utc(timestamp: str) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        _record(timestamp=timestamp)


def test_from_dict_rejects_unknown_or_missing_schema_fields() -> None:
    raw = json.loads(_record().to_jsonl())
    raw["extra"] = "no"
    with pytest.raises(ReconciliationLedgerCorruptError, match="schema mismatch"):
        ReconciliationRecord.from_dict(raw)

    raw = json.loads(_record().to_jsonl())
    raw.pop("operator_id")
    with pytest.raises(ReconciliationLedgerCorruptError, match="schema mismatch"):
        ReconciliationRecord.from_dict(raw)


def test_append_and_read_one_terminal_fact(tmp_path: Path) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    ledger.append_durable(record)
    assert ledger.read_records() == [record]


def test_exact_same_fact_retry_is_idempotent_even_with_new_timestamp(tmp_path: Path) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    first = _record(timestamp="2026-09-26T00:01:00+00:00")
    same_fact = _record(timestamp="2026-09-26T00:02:00+00:00")

    ledger.append_durable(first)
    ledger.append_durable(same_fact)

    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].timestamp == first.timestamp


def test_second_terminal_fact_for_same_effect_is_rejected(tmp_path: Path) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    ledger.append_durable(_record(reconciliation_id="rec-a"))

    with pytest.raises(ReconciliationLedgerCorruptError, match="already has"):
        ledger.append_durable(
            _record(
                reconciliation_id="rec-b",
                verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
            )
        )


def test_reconciliation_id_collision_across_effects_is_rejected(tmp_path: Path) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    ledger.append_durable(_record(reconciliation_id="global-id", effect_id="fx-a"))

    with pytest.raises(ReconciliationLedgerCorruptError, match="collides"):
        ledger.append_durable(
            _record(reconciliation_id="global-id", effect_id="fx-b")
        )


def test_duplicate_rows_already_on_disk_are_corruption(tmp_path: Path) -> None:
    path = tmp_path / "reconciliations.ndjson"
    record = _record()
    path.write_text(record.to_jsonl() + "\n" + record.to_jsonl() + "\n", encoding="utf-8")
    ledger = ReconciliationLedger(path=path)

    with pytest.raises(ReconciliationLedgerCorruptError, match="appears more than once"):
        ledger.read_records()


def test_same_path_instances_share_writer_lock_and_ambiguity_state(tmp_path: Path) -> None:
    path = tmp_path / "reconciliations.ndjson"
    first = ReconciliationLedger(path=path)
    second = ReconciliationLedger(path=path)
    assert first._lock is second._lock
    assert first._path_state is second._path_state


def test_conflicting_concurrent_terminal_writes_have_one_winner(tmp_path: Path) -> None:
    path = tmp_path / "reconciliations.ndjson"
    first = ReconciliationLedger(path=path)
    second = ReconciliationLedger(path=path)
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def append(ledger: ReconciliationLedger, record: ReconciliationRecord) -> None:
        barrier.wait()
        try:
            ledger.append_durable(record)
            outcome = "committed"
        except ReconciliationLedgerCorruptError:
            outcome = "rejected"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(target=append, args=(first, _record(reconciliation_id="a"))),
        threading.Thread(
            target=append,
            args=(
                second,
                _record(
                    reconciliation_id="b",
                    verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
                ),
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(outcomes) == ["committed", "rejected"]
    assert len(first.read_records()) == 1
