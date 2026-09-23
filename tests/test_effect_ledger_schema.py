"""Schema and writer-serialization regressions for the M5 EffectLedger."""

from __future__ import annotations

import json
import math
import threading
from pathlib import Path

import pytest

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerCorruptError,
    EffectLedgerRecord,
    EffectState,
)


def _record(state: EffectState, *, details: dict | None = None) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id="fx-schema",
        semantic_key="@actor|post|post|123|schema",
        state=state,
        action_type="post",
        intent_hash="intent-schema",
        policy_binding="policy-schema",
        actor_id="@actor",
        target_type="post",
        target_id="123",
        details=details or {},
    )


@pytest.mark.parametrize("field_name", ["actor_id", "target_type", "target_id"])
@pytest.mark.parametrize("bad", ["", 123, True])
def test_optional_lineage_fields_must_be_none_or_nonempty_strings(
    field_name: str,
    bad: object,
) -> None:
    kwargs = {
        "effect_id": "fx",
        "semantic_key": "key",
        "state": EffectState.RESERVED,
        "action_type": "post",
        "intent_hash": "intent",
        "policy_binding": "policy",
        "actor_id": "@actor",
        "target_type": "post",
        "target_id": "123",
    }
    kwargs[field_name] = bad
    with pytest.raises(ValueError, match=field_name):
        EffectLedgerRecord(**kwargs)  # type: ignore[arg-type]


def test_direct_record_requires_effect_state() -> None:
    with pytest.raises(ValueError, match="must be an EffectState"):
        EffectLedgerRecord(
            effect_id="fx",
            semantic_key="key",
            state="RESERVED",  # type: ignore[arg-type]
            action_type="post",
            intent_hash="intent",
            policy_binding="policy",
        )


def test_direct_record_requires_dict_details() -> None:
    with pytest.raises(ValueError, match="details.*dict"):
        EffectLedgerRecord(
            effect_id="fx",
            semantic_key="key",
            state=EffectState.RESERVED,
            action_type="post",
            intent_hash="intent",
            policy_binding="policy",
            details=[],  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("bad", [None, [], "", 0, False])
def test_from_dict_rejects_falsy_non_object_details(bad: object) -> None:
    raw = json.loads(_record(EffectState.RESERVED).to_jsonl())
    raw["details"] = bad
    with pytest.raises(EffectLedgerCorruptError, match="details must be an object"):
        EffectLedgerRecord.from_dict(raw)


def test_from_dict_allows_missing_details_as_empty_object() -> None:
    raw = json.loads(_record(EffectState.RESERVED).to_jsonl())
    raw.pop("details")
    parsed = EffectLedgerRecord.from_dict(raw)
    assert parsed.details == {}


@pytest.mark.parametrize(
    "details",
    [
        {1: "numeric key"},
        {"bad": object()},
        {"bad": ("tuple",)},
        {"bad": {"nested": object()}},
        {"bad": float("nan")},
        {"bad": float("inf")},
        {"bad": -float("inf")},
    ],
)
def test_details_reject_values_json_would_coerce_or_encode_nonportably(
    details: dict,
) -> None:
    with pytest.raises(ValueError, match="details"):
        _record(EffectState.RESERVED, details=details)


def test_details_allow_nested_strict_json_values() -> None:
    record = _record(
        EffectState.RESERVED,
        details={
            "proof": {
                "ok": True,
                "count": 2,
                "ratio": 0.5,
                "optional": None,
                "items": ["a", 1, False, {"nested": "yes"}],
            }
        },
    )
    encoded = record.to_jsonl()
    parsed = EffectLedgerRecord.from_dict(json.loads(encoded))
    assert parsed.details == record.details
    assert math.isfinite(parsed.details["proof"]["ratio"])


@pytest.mark.parametrize("bad", [None, "", 123, True])
def test_timestamp_must_be_nonempty_string(bad: object) -> None:
    with pytest.raises(ValueError, match="timestamp"):
        EffectLedgerRecord(
            effect_id="fx",
            semantic_key="key",
            state=EffectState.RESERVED,
            action_type="post",
            intent_hash="intent",
            policy_binding="policy",
            timestamp=bad,  # type: ignore[arg-type]
        )


def test_from_dict_applies_optional_lineage_schema() -> None:
    raw = json.loads(_record(EffectState.RESERVED).to_jsonl())
    raw["actor_id"] = 123
    with pytest.raises(EffectLedgerCorruptError, match="actor_id"):
        EffectLedgerRecord.from_dict(raw)


def test_same_path_instances_share_process_local_writer_lock(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    first = EffectLedger(path=path)
    second = EffectLedger(path=path)
    assert first._lock is second._lock


def test_conflicting_concurrent_terminal_writes_cannot_both_commit(
    tmp_path: Path,
) -> None:
    path = tmp_path / "effects.ndjson"
    first = EffectLedger(path=path)
    second = EffectLedger(path=path)
    first.append_durable(_record(EffectState.RESERVED))

    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcomes_lock = threading.Lock()

    def append_terminal(ledger: EffectLedger, state: EffectState) -> None:
        barrier.wait()
        try:
            ledger.append_durable(_record(state))
            outcome = f"committed:{state.value}"
        except EffectLedgerCorruptError:
            outcome = f"rejected:{state.value}"
        with outcomes_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(
            target=append_terminal,
            args=(first, EffectState.EFFECT_CONFIRMED),
        ),
        threading.Thread(
            target=append_terminal,
            args=(second, EffectState.EFFECT_UNKNOWN),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sum(item.startswith("committed:") for item in outcomes) == 1
    assert sum(item.startswith("rejected:") for item in outcomes) == 1
    records = first.read_records()
    assert [record.state for record in records[:1]] == [EffectState.RESERVED]
    assert len(records) == 2
    assert records[-1].state in {
        EffectState.EFFECT_CONFIRMED,
        EffectState.EFFECT_UNKNOWN,
    }
