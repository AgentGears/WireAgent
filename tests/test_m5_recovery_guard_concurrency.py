"""Concurrency regression for Layer-6 RecoveryGuard snapshot publication."""

from __future__ import annotations

import threading
from pathlib import Path

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    RecoveryProjection,
)
from webwire.safety.recovery_guard import RecoveryGuard


class _BlockingProjectionLedger(EffectLedger):
    """Hold the first projection so a second refresh attempts to overtake it."""

    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.first_entered = threading.Event()
        self.release_first = threading.Event()
        self.second_entered = threading.Event()
        self._calls_lock = threading.Lock()
        self._calls = 0

    def recovery_projection(self) -> list[RecoveryProjection]:
        with self._calls_lock:
            self._calls += 1
            call = self._calls
        if call == 1:
            self.first_entered.set()
            assert self.release_first.wait(timeout=2.0)
            return []
        self.second_entered.set()
        return super().recovery_projection()


def test_refresh_serializes_read_and_publication(tmp_path: Path) -> None:
    ledger = _BlockingProjectionLedger(path=tmp_path / "effects.ndjson")
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

    # Durable truth advances while the first guard refresh still owns an older
    # conceptual snapshot. A second refresh must not enter the ledger until the
    # first read+publication transaction has completed.
    ledger.append_durable(
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
