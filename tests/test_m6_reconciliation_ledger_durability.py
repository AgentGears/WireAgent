"""M6 Layer-1 durability ambiguity and startup qualification regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.safety.reconciliation_ledger import (
    ReconciliationLedger,
    ReconciliationLedgerAmbiguousError,
    ReconciliationLedgerCorruptError,
    ReconciliationLedgerError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)


def _record(
    *,
    reconciliation_id: str = "rec-durable",
    effect_id: str = "fx-durable",
    verdict: ReconciliationVerdict = ReconciliationVerdict.CONFIRMED_NO_EFFECT,
    basis: str = "durability-test",
) -> ReconciliationRecord:
    evidence = {
        "basis": basis,
        "observed_at": "2026-09-26T00:00:00+00:00",
        "observations": [{"kind": "test", "value": "stable"}],
    }
    return ReconciliationRecord(
        reconciliation_id=reconciliation_id,
        effect_id=effect_id,
        semantic_key="@actor|post|post|123|durable",
        action_type="post",
        intent_hash="intent-durable",
        policy_binding="policy-durable",
        actor_id="@actor",
        target_type="post",
        target_id="123",
        verdict=verdict,
        operator_id="operator-local",
        evidence_hash=canonical_evidence_hash(evidence),
        evidence=evidence,
        timestamp="2026-09-26T00:01:00+00:00",
    )


def _fail_next_fsync_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)


def test_written_row_with_failed_file_fsync_latches_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(ReconciliationLedgerError, match="append failed"):
        ledger.append_durable(record)

    assert ledger.path.exists()
    assert ledger.durability_ambiguous is True
    with pytest.raises(ReconciliationLedgerAmbiguousError):
        ledger.read_records()


def test_same_path_instance_observes_ambiguity_and_blocks_different_fact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliations.ndjson"
    first = ReconciliationLedger(path=path)
    second = ReconciliationLedger(path=path)
    record = _record()
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(ReconciliationLedgerError):
        first.append_durable(record)

    assert second.durability_ambiguous is True
    with pytest.raises(ReconciliationLedgerAmbiguousError, match="different fact"):
        second.append_durable(
            _record(
                reconciliation_id="rec-other",
                effect_id="fx-other",
                basis="other",
            )
        )


def test_exact_retry_redurables_visible_fact_and_clears_latch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliations.ndjson"
    first = ReconciliationLedger(path=path)
    second = ReconciliationLedger(path=path)
    record = _record()
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(ReconciliationLedgerError):
        first.append_durable(record)

    # The first injected call has already failed; subsequent fsyncs use the real
    # primitive. The second object shares the exact ambiguity latch.
    second.append_durable(record)

    assert first.durability_ambiguous is False
    records = first.read_records()
    assert records == [record]
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1


def test_exact_retry_uses_writable_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliations.ndjson"
    ledger = ReconciliationLedger(path=path)
    record = _record()
    ledger.append_durable(record)

    real_open = os.open
    seen_flags: list[int] = []

    def tracking_open(path_value, flags, *args):  # type: ignore[no-untyped-def]
        if Path(path_value) == ledger.path:
            seen_flags.append(flags)
        return real_open(path_value, flags, *args)

    monkeypatch.setattr(os, "open", tracking_open)
    ledger.append_durable(record)

    assert seen_flags
    assert seen_flags[-1] & os.O_RDWR == os.O_RDWR
    assert len(ledger.read_records()) == 1


def test_first_entry_directory_fsync_failure_latches_ambiguity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    real_fsync_directory = ReconciliationLedger._fsync_directory
    failed = False

    def fail_parent(path: Path) -> None:
        nonlocal failed
        if path == ledger.path.parent and not failed:
            failed = True
            raise OSError("injected directory fsync failure")
        real_fsync_directory(path)

    monkeypatch.setattr(ReconciliationLedger, "_fsync_directory", staticmethod(fail_parent))

    with pytest.raises(ReconciliationLedgerError, match="directory fsync failed"):
        ledger.append_durable(record)

    assert ledger.durability_ambiguous is True


def test_clean_open_failure_before_any_bytes_does_not_latch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    real_open = os.open

    def fail_append_open(path_value, flags, *args):  # type: ignore[no-untyped-def]
        if Path(path_value) == ledger.path and flags & os.O_APPEND:
            raise OSError("injected clean open failure")
        return real_open(path_value, flags, *args)

    monkeypatch.setattr(os, "open", fail_append_open)

    with pytest.raises(ReconciliationLedgerError, match="append failed"):
        ledger.append_durable(_record())

    assert ledger.durability_ambiguous is False
    assert ledger.read_records() == []


def test_partial_write_latches_and_torn_tail_is_never_repaired_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    real_write = os.write
    calls = 0

    def partial_then_fail(fd: int, data) -> int:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, bytes(data[:16]))
        raise OSError("injected partial write failure")

    monkeypatch.setattr(os, "write", partial_then_fail)

    with pytest.raises(ReconciliationLedgerError, match="append failed"):
        ledger.append_durable(record)

    assert ledger.durability_ambiguous is True
    with pytest.raises(ReconciliationLedgerCorruptError, match="not valid JSON"):
        ledger.append_durable(record)
    assert ledger.durability_ambiguous is True


def test_authoritative_startup_read_fsyncs_existing_file_with_writable_handle(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliations.ndjson"
    record = _record()
    path.write_text(record.to_jsonl() + "\n", encoding="utf-8")
    ledger = ReconciliationLedger(path=path)

    real_open = os.open
    seen_flags: list[int] = []

    def tracking_open(path_value, flags, *args):  # type: ignore[no-untyped-def]
        if Path(path_value) == ledger.path:
            seen_flags.append(flags)
        return real_open(path_value, flags, *args)

    monkeypatch.setattr(os, "open", tracking_open)
    records = ledger.read_authoritative()

    assert records == [record]
    assert seen_flags
    assert seen_flags[-1] & os.O_RDWR == os.O_RDWR


def test_authoritative_startup_fsync_failure_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    path = tmp_path / "reconciliations.ndjson"
    record = _record()
    path.write_text(record.to_jsonl() + "\n", encoding="utf-8")
    ledger = ReconciliationLedger(path=path)
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(ReconciliationLedgerError, match="re-durability fsync failed"):
        ledger.read_authoritative()


def test_missing_reconciliation_file_is_valid_empty_authoritative_history(
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "missing.ndjson")
    assert ledger.read_authoritative() == []


def test_local_ambiguity_cannot_be_cleared_by_authoritative_read(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    _fail_next_fsync_once(monkeypatch)
    with pytest.raises(ReconciliationLedgerError):
        ledger.append_durable(_record())

    with pytest.raises(ReconciliationLedgerAmbiguousError):
        ledger.read_authoritative()
    assert ledger.durability_ambiguous is True
