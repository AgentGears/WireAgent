"""Concurrency regression for RecoveryGuard snapshot publication ordering."""

from __future__ import annotations

import threading
from pathlib import Path

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.recovery_guard import RecoveryGuard


class _BlockingSnapshotLedger(EffectLedger):
    """Hold the first canonical history read after capturing its old snapshot."""

    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.second_entered = threading.Event()
        self._calls_lock = threading.Lock()
        self._calls = 0

    def read_records(self) -> list[EffectLedgerRecord]:
        records = super().read_records()
        with self._calls_lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_entered.set()
            assert self.release_first.wait(timeout=2.0)
            return records
        self.second_entered.set()
        return records


def test_refresh_serializes_composite_read_and_publication(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    ledger = _BlockingSnapshotLedger(path=path)
    writer = EffectLedger(path=path)
    guard = RecoveryGuard(ledger)
    key = "alice|like|post|123|"
    errors: list[BaseException] = []

    def refresh() -> None:
        try:
            guard.refresh()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first = threading.Thread(target=refresh)
    first.start()
    assert ledger.first_entered.wait(timeout=2.0)

    # The first refresh already captured an empty effect snapshot and is held
    # before its reconciliation read/publication. Durable truth now advances
    # through a separate object sharing the same effect-ledger path lock.
    writer.append_durable(
        EffectLedgerRecord(
            effect_id="effect-unknown",
            semantic_key=key,
            state=EffectState.EFFECT_UNKNOWN,
            action_type="like",
            intent_hash="intent-hash",
            policy_binding="policy-binding",
            actor_id="alice",
            target_type="post",
            target_id="123",
        )
    )

    second = threading.Thread(target=refresh)
    second.start()

    # The shared publication fence covers read -> join -> publication, so the
    # second refresh cannot enter its effect-history read and later be overtaken
    # by the first older clear publication.
    assert ledger.second_entered.wait(timeout=0.05) is False

    ledger.release_first.set()
    first.join(timeout=2.0)
    second.join(timeout=2.0)

    assert errors == []
    assert first.is_alive() is False
    assert second.is_alive() is False
    assert ledger.second_entered.is_set()

    block = guard.require_clear(key, refresh=False)
    assert block is not None
    assert block.effect_ids == ("effect-unknown",)
