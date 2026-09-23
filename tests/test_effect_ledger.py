"""M5 layer-1 fault-injection tests for the durable EffectLedger."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

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
    action_type: str = "post",
    intent_hash: str = "intent-123",
    policy_binding: str = "policy-123",
    actor_id: Optional[str] = "@actor",
    target_type: Optional[str] = "post",
    target_id: Optional[str] = "123",
) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=semantic_key,
        state=state,
        action_type=action_type,
        intent_hash=intent_hash,
        policy_binding=policy_binding,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
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


def test_no_effect_resolves_prior_reservation(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))
    ledger.append_durable(_record(EffectState.NO_EFFECT))

    item = ledger.recovery_projection()[0]
    assert item.raw_state is EffectState.NO_EFFECT
    assert item.effective_state is EffectState.NO_EFFECT
    assert item.unresolved is False


def test_best_effort_terminal_fact_may_be_initial_record(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(
        _record(EffectState.EFFECT_CONFIRMED, effect_id="confirmed")
    )
    ledger.append_durable(
        _record(EffectState.EFFECT_UNKNOWN, effect_id="unknown")
    )

    projected = {item.effect_id: item for item in ledger.recovery_projection()}
    assert projected["confirmed"].unresolved is False
    assert projected["unknown"].unresolved is True


def test_no_effect_cannot_be_initial_durable_fact(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    with pytest.raises(EffectLedgerCorruptError, match="illegal initial state NO_EFFECT"):
        ledger.append_durable(_record(EffectState.NO_EFFECT))
    assert not ledger.path.exists()


def test_terminal_state_cannot_be_reopened(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))
    ledger.append_durable(_record(EffectState.EFFECT_CONFIRMED))
    before = ledger.path.read_bytes()

    with pytest.raises(
        EffectLedgerCorruptError,
        match="EFFECT_CONFIRMED -> RESERVED",
    ):
        ledger.append_durable(_record(EffectState.RESERVED))

    assert ledger.path.read_bytes() == before


def test_conflicting_terminal_outcome_is_rejected_before_append(tmp_path: Path) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    ledger.append_durable(_record(EffectState.RESERVED))
    ledger.append_durable(_record(EffectState.EFFECT_CONFIRMED))
    before = ledger.path.read_bytes()

    with pytest.raises(
        EffectLedgerCorruptError,
        match="EFFECT_CONFIRMED -> EFFECT_UNKNOWN",
    ):
        ledger.append_durable(_record(EffectState.EFFECT_UNKNOWN))

    assert ledger.path.read_bytes() == before


@pytest.mark.parametrize(
    ("field_name", "changed"),
    [
        ("semantic_key", "changed-key"),
        ("action_type", "reply"),
        ("intent_hash", "changed-intent"),
        ("policy_binding", "changed-policy"),
        ("actor_id", "@other"),
        ("target_type", "user"),
        ("target_id", "999"),
    ],
)
def test_effect_lineage_is_immutable(
    tmp_path: Path,
    field_name: str,
    changed: object,
) -> None:
    ledger = EffectLedger(WebWireConfig(state_dir=tmp_path))
    first = _record(EffectState.RESERVED)
    ledger.append_durable(first)
    kwargs = {
        "semantic_key": first.semantic_key,
        "action_type": first.action_type,
        "intent_hash": first.intent_hash,
        "policy_binding": first.policy_binding,
        "actor_id": first.actor_id,
        "target_type": first.target_type,
        "target_id": first.target_id,
    }
    kwargs[field_name] = changed

    with pytest.raises(EffectLedgerCorruptError, match=f"changed {field_name}"):
        ledger.append_durable(_record(EffectState.EFFECT_UNKNOWN, **kwargs))


def test_reader_rejects_manually_persisted_impossible_history(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    rows = [
        _record(EffectState.RESERVED),
        _record(EffectState.EFFECT_CONFIRMED),
        _record(EffectState.EFFECT_UNKNOWN),
    ]
    path.write_text(
        "\n".join(row.to_jsonl() for row in rows) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        EffectLedgerCorruptError,
        match="EFFECT_CONFIRMED -> EFFECT_UNKNOWN",
    ):
        EffectLedger(path=path).recovery_projection()


# ---------------------------------------------------------------------------
# Codex review regressions (PR #2, 2026-09-23)
# ---------------------------------------------------------------------------

def test_from_dict_rejects_non_string_identities() -> None:
    """P2: syntactically valid non-string identity fields must fail closed."""
    good = EffectLedgerRecord(
        effect_id="e1",
        semantic_key="k1",
        state=EffectState.RESERVED,
        action_type="post",
        intent_hash="h" * 32,
        policy_binding="p" * 32,
    )
    base = json.loads(good.to_jsonl())
    for field in (
        "effect_id",
        "semantic_key",
        "action_type",
        "intent_hash",
        "policy_binding",
    ):
        for bad in (None, 123, 4.5, True):
            raw = dict(base)
            raw[field] = bad
            with pytest.raises(EffectLedgerCorruptError):
                EffectLedgerRecord.from_dict(raw)


def test_validate_rejects_non_string_identity_direct_construction() -> None:
    """In-code records are held to the same standard as parsed ones."""
    with pytest.raises(ValueError, match="non-empty string"):
        EffectLedgerRecord(
            effect_id=None,  # type: ignore[arg-type]
            semantic_key="k",
            state=EffectState.NO_EFFECT,
            action_type="post",
            intent_hash="h",
            policy_binding="p",
        )


def test_fsync_directory_is_a_windows_no_op(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Win32 has no portable directory-fsync primitive; do not open the dir."""

    def _no_opens(fd_or_path, *a, **k):  # type: ignore[no-untyped-def]  # pragma: no cover
        raise AssertionError("os.open must not be called on Windows path")

    monkeypatch.setattr("os.name", "nt")
    monkeypatch.setattr("os.open", _no_opens)
    EffectLedger._fsync_directory(tmp_path)
