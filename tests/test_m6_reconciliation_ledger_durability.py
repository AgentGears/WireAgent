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

    monkeypatch.setattr(
        ReconciliationLedger,
        "_fsync_directory",
        staticmethod(fail_parent),
    )

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


def test_zero_byte_write_failure_after_ocreat_does_not_skip_later_directory_fsync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    real_write = os.write

    def fail_before_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
        raise OSError("injected zero-byte write failure")

    monkeypatch.setattr(os, "write", fail_before_write)
    with pytest.raises(ReconciliationLedgerError, match="append failed"):
        ledger.append_durable(record)

    # O_CREAT happened, so an empty file may now exist, but no fact bytes are
    # ambiguous. The subsequent successful append still has to durabilize the
    # parent entry instead of treating file existence as proof that it was done.
    assert ledger.path.exists()
    assert ledger.path.stat().st_size == 0
    assert ledger.durability_ambiguous is False

    monkeypatch.setattr(os, "write", real_write)
    real_fsync_directory = ReconciliationLedger._fsync_directory
    parent_calls: list[Path] = []

    def track_directory(path: Path) -> None:
        parent_calls.append(path)
        real_fsync_directory(path)

    monkeypatch.setattr(
        ReconciliationLedger,
        "_fsync_directory",
        staticmethod(track_directory),
    )
    ledger.append_durable(record)

    assert ledger.path.parent in parent_calls
    assert ledger.read_records() == [record]


def test_short_writes_are_completed_before_fsync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    record = _record()
    real_write = os.write
    calls = 0

    def short_write(fd: int, data) -> int:  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        chunk = max(1, len(data) // 2)
        return real_write(fd, bytes(data[:chunk]))

    monkeypatch.setattr(os, "write", short_write)
    ledger.append_durable(record)

    assert calls > 1
    assert ledger.read_records() == [record]


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
    with pytest.raises(ReconciliationLedgerCorruptError, match="torn tail"):
        ledger.append_durable(record)
    assert ledger.durability_ambiguous is True


def test_complete_json_without_terminal_newline_is_torn_corruption(tmp_path: Path) -> None:
    path = tmp_path / "reconciliations.ndjson"
    path.write_text(_record().to_jsonl(), encoding="utf-8")
    ledger = ReconciliationLedger(path=path)

    with pytest.raises(ReconciliationLedgerCorruptError, match="torn tail"):
        ledger.read_records()


def test_non_utf8_ledger_is_corruption(tmp_path: Path) -> None:
    path = tmp_path / "reconciliations.ndjson"
    path.write_bytes(b"\xff\n")
    ledger = ReconciliationLedger(path=path)

    with pytest.raises(ReconciliationLedgerCorruptError, match="valid UTF-8"):
        ledger.read_records()


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
