"""M5 layer-1 fault-injection tests for the durable EffectLedger."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerCorruptError,
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
)


def _record(
    state: EffectState,
    *,
    effect_id: str = "fx-1",
    semantic_key: str = "@actor|post|post|123|abc",
) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=semantic_key,
        state=state,
        action_type="post",
        intent_hash="intent-123",
        policy_binding="policy-123",
        actor_id="@actor",
        target_type="post",
        target_id="123",
    )


def test_effects_path_is_separate_from_audit_journal(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    assert cfg.effects_path() == tmp_path / "effects.ndjson"
    assert cfg.effects_path() != cfg.journal_path()


def test_append_durable_writes_valid_ndjson(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))

    path = tmp_path / "effects.ndjson"
    assert path.exists()
    raw = json.loads(path.read_text(encoding="utf-8").strip())
    assert raw["state"] == "RESERVED"
    assert raw["effect_id"] == "fx-1"


def test_append_durable_calls_fsync(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", tracking_fsync)
    EffectLedger(WebWireConfig(state_dir=tmp_path)).append_durable(
        _record(EffectState.RESERVED)
    )
    # File fsync always; POSIX also fsyncs the new file's directory entry.
    assert len(calls) >= (1 if os.name == "nt" else 2)


def test_new_state_directory_ancestry_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "new" / "state"
    fsynced_dirs: list[Path] = []

    def record_dir_fsync(path: Path) -> None:
        fsynced_dirs.append(path)

    monkeypatch.setattr(
        EffectLedger,
        "_fsync_directory",
        staticmethod(record_dir_fsync),
    )
    EffectLedger(WebWireConfig(state_dir=state_dir)).append_durable(
        _record(EffectState.RESERVED)
    )

    # The newly created state directory and the entries that name its ancestry
    # are explicitly sent through the directory-durability hook.
    assert state_dir in fsynced_dirs
    assert state_dir.parent in fsynced_dirs
    assert tmp_path in fsynced_dirs


def test_append_durable_fsync_failure_raises_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_fsync(_fd: int) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(os, "fsync", fail_fsync)
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    with pytest.raises(EffectLedgerError, match="append failed"):
        ledger.append_durable(_record(EffectState.RESERVED))


def test_read_corrupt_tail_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    path.write_text(
        _record(EffectState.RESERVED).to_jsonl() + "\n" + '{"effect_id":',
        encoding="utf-8",
    )
    ledger = EffectLedger(path=path)
    with pytest.raises(EffectLedgerCorruptError, match="not valid JSON"):
        ledger.read_records()


def test_reserved_projects_as_unknown_and_unresolved(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))

    projection = ledger.recovery_projection()
    assert len(projection) == 1
    item = projection[0]
    assert item.raw_state == EffectState.RESERVED
    assert item.effective_state == EffectState.EFFECT_UNKNOWN
    assert item.unresolved is True
    assert ledger.unresolved_semantic_keys() == {"@actor|post|post|123|abc"}

    # Projection must not rewrite raw evidence.
    records = ledger.read_records()
    assert records[-1].state == EffectState.RESERVED


def test_terminal_state_resolves_prior_reservation(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))
    ledger.append_durable(_record(EffectState.EFFECT_CONFIRMED))

    item = ledger.recovery_projection()[0]
    assert item.raw_state == EffectState.EFFECT_CONFIRMED
    assert item.effective_state == EffectState.EFFECT_CONFIRMED
    assert item.unresolved is False
    assert ledger.unresolved_semantic_keys() == set()


def test_unknown_outcome_remains_unresolved(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))
    ledger.append_durable(_record(EffectState.EFFECT_UNKNOWN))

    item = ledger.recovery_projection()[0]
    assert item.unresolved is True
    assert item.effective_state == EffectState.EFFECT_UNKNOWN


def test_effect_identity_cannot_change_semantic_key(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED, semantic_key="key-a"))
    ledger.append_durable(_record(EffectState.EFFECT_UNKNOWN, semantic_key="key-b"))

    with pytest.raises(EffectLedgerCorruptError, match="changed semantic_key"):
        ledger.recovery_projection()


# ---------------------------------------------------------------------------
# Codex review regressions (PR #2, 2026-09-23)
# ---------------------------------------------------------------------------

def test_from_dict_rejects_non_string_identities() -> None:
    """P2: a syntactically valid record whose identity fields are null or
    numbers must FAIL CLOSED, not coerce to 'None'/'123' and hydrate."""
    from webwire.safety.effect_ledger import EffectLedgerRecord
    good = EffectLedgerRecord(
        effect_id="e1", semantic_key="k1", state=EffectState.RESERVED,
        action_type="post", intent_hash="h" * 32, policy_binding="p" * 32,
    )
    base = json.loads(good.to_jsonl())
    for field in ("effect_id", "semantic_key", "action_type", "intent_hash", "policy_binding"):
        for bad in (None, 123, 4.5, True):
            raw = dict(base)
            raw[field] = bad
            with pytest.raises(EffectLedgerCorruptError):
                EffectLedgerRecord.from_dict(raw)


def test_validate_rejects_non_string_identity_direct_construction() -> None:
    """In-code records are held to the same standard as parsed ones."""
    with pytest.raises(ValueError, match="non-empty string"):
        EffectLedgerRecord(
            effect_id=None, semantic_key="k", state=EffectState.NO_EFFECT,
            action_type="post", intent_hash="h", policy_binding="p",
        )


def test_fsync_directory_is_a_windows_no_op(monkeypatch: pytest.MonkeyPatch,
                                             tmp_path: Path) -> None:
    """P1 regression lock: on win32 the directory-fsync primitive does not
    exist; _fsync_directory must return WITHOUT attempting os.open on the
    directory (which would raise and poison a committed reservation)."""
    from webwire.safety.effect_ledger import EffectLedger

    def _no_opens(fd_or_path, *a, **k):  # pragma: no cover - must not run
        raise AssertionError("os.open must not be called on Windows path")

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("os.open", _no_opens)
    EffectLedger._fsync_directory(tmp_path)  # must silently no-op
