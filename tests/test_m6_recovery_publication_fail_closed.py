"""M6 Layer-2 guard publication ordering and cache fail-closed regressions."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from webwire.safety import (
    EffectLedger,
    ReconciliationLedger,
    ReconciliationRecord,
    RecoveryGuard,
    RecoveryGuardUnavailable,
)


class _BlockingReconciliationLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def read_authoritative(self) -> list[ReconciliationRecord]:
        self.entered.set()
        assert self.release.wait(timeout=2.0)
        return super().read_authoritative()


def test_guard_cache_lock_is_not_held_during_durable_projection_read(
    tmp_path: Path,
) -> None:
    reconciliations = _BlockingReconciliationLedger(
        path=tmp_path / "reconciliations.ndjson"
    )
    guard = RecoveryGuard(
        EffectLedger(path=tmp_path / "effects.ndjson"),
        reconciliation_ledger=reconciliations,
    )
    refresh_errors: list[BaseException] = []

    def refresh() -> None:
        try:
            guard.refresh()
        except BaseException as exc:  # pragma: no cover - surfaced below
            refresh_errors.append(exc)

    refresh_thread = threading.Thread(target=refresh)
    refresh_thread.start()
    assert reconciliations.entered.wait(timeout=2.0)

    status_done = threading.Event()

    def read_status() -> None:
        guard.status()
        status_done.set()

    status_thread = threading.Thread(target=read_status)
    status_thread.start()

    # Frozen M6 lock order is publication fence -> ledger/path locks -> guard
    # cache lock. A status read needs only the final cache lock and therefore
    # must not wait for a blocked durable-ledger read.
    assert status_done.wait(timeout=0.2)

    reconciliations.release.set()
    refresh_thread.join(timeout=2.0)
    status_thread.join(timeout=2.0)

    assert refresh_errors == []
    assert refresh_thread.is_alive() is False
    assert status_thread.is_alive() is False
    assert guard.status().available is True


class _ToggleFailureReconciliationLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.fail = False

    def read_authoritative(self) -> list[ReconciliationRecord]:
        if self.fail:
            raise RuntimeError("unexpected projector dependency failure")
        return super().read_authoritative()


def test_any_projection_exception_discards_previously_cached_clear_authority(
    tmp_path: Path,
) -> None:
    reconciliations = _ToggleFailureReconciliationLedger(
        path=tmp_path / "reconciliations.ndjson"
    )
    guard = RecoveryGuard(
        EffectLedger(path=tmp_path / "effects.ndjson"),
        reconciliation_ledger=reconciliations,
    )
    initial = guard.hydrate()
    assert initial.available is True
    assert initial.unresolved_semantic_keys == ()

    reconciliations.fail = True
    with pytest.raises(RecoveryGuardUnavailable):
        guard.refresh()

    status = guard.status()
    assert status.hydrated is True
    assert status.available is False
    assert status.unresolved_semantic_keys == ()
    assert status.error is not None
    assert "RuntimeError" in status.error

    with pytest.raises(RecoveryGuardUnavailable):
        guard.require_clear("alice|like|post|123|", refresh=False)
