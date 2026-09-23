"""Ambiguous durable-write retry regressions for EffectLedger."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

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
    details: dict | None = None,
) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id="fx-retry",
        semantic_key="@actor|post|post|123|retry",
        state=state,
        action_type="post",
        intent_hash="intent-retry",
        policy_binding="policy-retry",
        actor_id="@actor",
        target_type="post",
        target_id="123",
        details=details or {},
    )


def _fail_next_fsync_once(monkeypatch: pytest.MonkeyPatch) -> None:
    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected ambiguous fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)


def test_retry_same_initial_reservation_redurables_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    reservation = _record(EffectState.RESERVED)
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(EffectLedgerError, match="append failed"):
        ledger.append_durable(reservation)

    # os.write happened before the injected fsync error, so the row is visible
    # but durability is ambiguous. Retrying the exact fact must fsync it again,
    # not append RESERVED a second time.
    assert ledger.path.exists()
    ledger.append_durable(reservation)

    records = ledger.read_records()
    assert len(records) == 1
    assert records[0].state is EffectState.RESERVED


def test_retry_same_terminal_fact_redurables_without_duplicate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    ledger.append_durable(_record(EffectState.RESERVED))
    terminal = _record(
        EffectState.EFFECT_CONFIRMED,
        details={"posted_url": "https://x.com/a/status/1"},
    )
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(EffectLedgerError, match="append failed"):
        ledger.append_durable(terminal)

    ledger.append_durable(terminal)
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]


def test_exact_fact_redurability_uses_writable_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    reservation = _record(EffectState.RESERVED)
    ledger.append_durable(reservation)

    real_open = os.open
    seen_flags: list[int] = []

    def tracking_open(path, flags, *args):  # type: ignore[no-untyped-def]
        if Path(path) == ledger.path:
            seen_flags.append(flags)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", tracking_open)
    ledger.append_durable(reservation)

    assert seen_flags
    assert seen_flags[-1] & os.O_RDWR == os.O_RDWR
    assert len(ledger.read_records()) == 1


def test_retry_with_changed_evidence_is_not_silently_accepted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    ledger.append_durable(_record(EffectState.RESERVED))
    first = _record(EffectState.EFFECT_CONFIRMED, details={"proof": "first"})
    _fail_next_fsync_once(monkeypatch)

    with pytest.raises(EffectLedgerError, match="append failed"):
        ledger.append_durable(first)

    changed = _record(EffectState.EFFECT_CONFIRMED, details={"proof": "changed"})
    with pytest.raises(
        EffectLedgerCorruptError,
        match="EFFECT_CONFIRMED -> EFFECT_CONFIRMED",
    ):
        ledger.append_durable(changed)
