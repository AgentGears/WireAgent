"""M6 Layer-1 raw JSON strictness regressions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webwire.safety.reconciliation_ledger import (
    ReconciliationLedger,
    ReconciliationLedgerCorruptError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)


def _record() -> ReconciliationRecord:
    evidence = {
        "basis": "strict-json",
        "observed_at": "2026-09-26T00:00:00+00:00",
        "observations": [{"kind": "stable", "value": True}],
    }
    return ReconciliationRecord(
        reconciliation_id="rec-strict",
        effect_id="fx-strict",
        semantic_key="@actor|post|post|strict|",
        action_type="post",
        intent_hash="intent-strict",
        policy_binding="policy-strict",
        actor_id="@actor",
        target_type="post",
        target_id="strict",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        operator_id="operator-local",
        evidence_hash=canonical_evidence_hash(evidence),
        evidence=evidence,
        timestamp="2026-09-26T00:01:00+00:00",
    )


def test_duplicate_top_level_json_key_is_corruption(tmp_path: Path) -> None:
    record = _record()
    line = record.to_jsonl()
    duplicate = line[:-1] + ',"effect_id":"shadow-effect"}'
    path = tmp_path / "reconciliations.ndjson"
    path.write_text(duplicate + "\n", encoding="utf-8")

    with pytest.raises(ReconciliationLedgerCorruptError, match="strict JSON"):
        ReconciliationLedger(path=path).read_records()


def test_duplicate_nested_evidence_key_is_corruption(tmp_path: Path) -> None:
    raw = json.loads(_record().to_jsonl())
    evidence = raw["evidence"]
    # Construct raw JSON deliberately; a Python dict cannot represent duplicate
    # keys, which is exactly why the ledger parser must reject them before loss.
    evidence_json = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    evidence_with_duplicate = evidence_json[:-1] + ',"basis":"shadow-basis"}'
    raw_without_evidence = dict(raw)
    raw_without_evidence.pop("evidence")
    prefix = json.dumps(raw_without_evidence, sort_keys=True, separators=(",", ":"))
    line = prefix[:-1] + ',"evidence":' + evidence_with_duplicate + "}"
    path = tmp_path / "reconciliations.ndjson"
    path.write_text(line + "\n", encoding="utf-8")

    with pytest.raises(ReconciliationLedgerCorruptError, match="strict JSON"):
        ReconciliationLedger(path=path).read_records()
