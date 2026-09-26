"""M6 Layer-2 composite recovery projection and guard regressions."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from webwire.safety import (
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationLedger,
    ReconciliationLedgerError,
    ReconciliationRecord,
    ReconciliationVerdict,
    RecoveryDisposition,
    RecoveryGuard,
    RecoveryGuardUnavailable,
    RecoveryProjector,
    RecoveryProjectorCorruptError,
    canonical_evidence_hash,
)


def _effect_record(
    *,
    effect_id: str,
    semantic_key: str = "alice|like|post|123|",
    state: EffectState = EffectState.EFFECT_UNKNOWN,
    action_type: str = "like",
    intent_hash: str = "intent-hash",
    policy_binding: str = "policy-binding",
    actor_id: str | None = "alice",
    target_type: str | None = "post",
    target_id: str | None = "123",
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
        timestamp="2026-09-26T06:00:00+00:00",
    )


def _evidence(label: str = "operator-resolution") -> dict:
    return {
        "basis": label,
        "observed_at": "2026-09-26T06:01:00+00:00",
        "observations": [{"kind": "operator", "value": "reviewed"}],
    }


def _reconciliation(
    *,
    effect: EffectLedgerRecord,
    reconciliation_id: str | None = None,
    verdict: ReconciliationVerdict = ReconciliationVerdict.CONFIRMED_EFFECT,
    **lineage_overrides: object,
) -> ReconciliationRecord:
    evidence = _evidence()
    lineage: dict[str, object] = {
        "semantic_key": effect.semantic_key,
        "action_type": effect.action_type,
        "intent_hash": effect.intent_hash,
        "policy_binding": effect.policy_binding,
        "actor_id": effect.actor_id,
        "target_type": effect.target_type,
        "target_id": effect.target_id,
    }
    lineage.update(lineage_overrides)
    return ReconciliationRecord(
        reconciliation_id=reconciliation_id or f"rec-{effect.effect_id}",
        effect_id=effect.effect_id,
        semantic_key=lineage["semantic_key"],  # type: ignore[arg-type]
        action_type=lineage["action_type"],  # type: ignore[arg-type]
        intent_hash=lineage["intent_hash"],  # type: ignore[arg-type]
        policy_binding=lineage["policy_binding"],  # type: ignore[arg-type]
        actor_id=lineage["actor_id"],  # type: ignore[arg-type]
        target_type=lineage["target_type"],  # type: ignore[arg-type]
        target_id=lineage["target_id"],  # type: ignore[arg-type]
        verdict=verdict,
        operator_id="operator-local",
        evidence_hash=canonical_evidence_hash(evidence),
        evidence=evidence,
        timestamp="2026-09-26T06:02:00+00:00",
    )


def _ledgers(tmp_path: Path) -> tuple[EffectLedger, ReconciliationLedger]:
    return (
        EffectLedger(path=tmp_path / "effects.ndjson"),
        ReconciliationLedger(path=tmp_path / "reconciliations.ndjson"),
    )


@pytest.mark.parametrize(
    ("raw_state", "verdict", "expected"),
    [
        (
            EffectState.RESERVED,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            RecoveryDisposition.RECONCILED_EFFECT,
        ),
        (
            EffectState.RESERVED,
            ReconciliationVerdict.CONFIRMED_NO_EFFECT,
            RecoveryDisposition.RECONCILED_NO_EFFECT,
        ),
        (
            EffectState.EFFECT_UNKNOWN,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            RecoveryDisposition.RECONCILED_EFFECT,
        ),
        (
            EffectState.EFFECT_UNKNOWN,
            ReconciliationVerdict.CONFIRMED_NO_EFFECT,
            RecoveryDisposition.RECONCILED_NO_EFFECT,
        ),
    ],
)
def test_terminal_reconciliation_clears_only_reconcilable_raw_states(
    tmp_path: Path,
    raw_state: EffectState,
    verdict: ReconciliationVerdict,
    expected: RecoveryDisposition,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    raw = _effect_record(effect_id="fx-1", state=raw_state)
    effects.append_durable(raw)
    reconciliations.append_durable(_reconciliation(effect=raw, verdict=verdict))

    projected = RecoveryProjector(effects, reconciliations).project()

    assert len(projected) == 1
    assert projected[0].raw_state is raw_state
    assert projected[0].disposition is expected
    assert projected[0].unresolved is False
    assert projected[0].reconciliation is not None


def test_no_reconciliation_preserves_m5_settled_and_unresolved_meaning(
    tmp_path: Path,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    effects.append_durable(
        _effect_record(effect_id="fx-reserved", state=EffectState.RESERVED)
    )
    effects.append_durable(
        _effect_record(effect_id="fx-unknown", state=EffectState.EFFECT_UNKNOWN)
    )
    effects.append_durable(
        _effect_record(effect_id="fx-confirmed", state=EffectState.EFFECT_CONFIRMED)
    )
    effects.append_durable(
        _effect_record(effect_id="fx-no-effect", state=EffectState.RESERVED)
    )
    effects.append_durable(
        _effect_record(effect_id="fx-no-effect", state=EffectState.NO_EFFECT)
    )

    by_id = {
        item.effect_id: item
        for item in RecoveryProjector(effects, reconciliations).project()
    }

    assert by_id["fx-reserved"].disposition is RecoveryDisposition.UNRESOLVED_UNKNOWN
    assert by_id["fx-reserved"].unresolved is True
    assert by_id["fx-unknown"].disposition is RecoveryDisposition.UNRESOLVED_UNKNOWN
    assert by_id["fx-unknown"].unresolved is True
    assert by_id["fx-confirmed"].disposition is RecoveryDisposition.SETTLED_EFFECT
    assert by_id["fx-confirmed"].unresolved is False
    assert by_id["fx-no-effect"].disposition is RecoveryDisposition.SETTLED_NO_EFFECT
    assert by_id["fx-no-effect"].unresolved is False


def test_reconciliation_does_not_rewrite_m5_historical_projection(tmp_path: Path) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    raw = _effect_record(effect_id="fx-1", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    reconciliations.append_durable(_reconciliation(effect=raw))

    historical = effects.recovery_projection()
    composite = RecoveryProjector(effects, reconciliations).project()

    assert historical[0].raw_state is EffectState.EFFECT_UNKNOWN
    assert historical[0].unresolved is True
    assert composite[0].disposition is RecoveryDisposition.RECONCILED_EFFECT
    assert composite[0].unresolved is False


def test_reconciliation_targeting_missing_effect_is_composite_corruption(
    tmp_path: Path,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    absent = _effect_record(effect_id="missing", state=EffectState.EFFECT_UNKNOWN)
    reconciliations.append_durable(_reconciliation(effect=absent))

    with pytest.raises(RecoveryProjectorCorruptError, match="missing effect_id"):
        RecoveryProjector(effects, reconciliations).project()

    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    with pytest.raises(RecoveryGuardUnavailable):
        guard.hydrate()
    assert guard.status().available is False


@pytest.mark.parametrize(
    "settled_state",
    [EffectState.NO_EFFECT, EffectState.EFFECT_CONFIRMED],
)
def test_reconciliation_targeting_settled_effect_is_corruption(
    tmp_path: Path,
    settled_state: EffectState,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    first = _effect_record(
        effect_id="fx-settled",
        state=(
            EffectState.RESERVED
            if settled_state is EffectState.NO_EFFECT
            else EffectState.EFFECT_CONFIRMED
        ),
    )
    effects.append_durable(first)
    if settled_state is EffectState.NO_EFFECT:
        effects.append_durable(
            _effect_record(effect_id="fx-settled", state=EffectState.NO_EFFECT)
        )
    reconciliations.append_durable(_reconciliation(effect=first))

    with pytest.raises(RecoveryProjectorCorruptError, match="already-settled"):
        RecoveryProjector(effects, reconciliations).project()


@pytest.mark.parametrize(
    ("field", "bad_value"),
    [
        ("semantic_key", "alice|like|post|999|"),
        ("action_type", "bookmark"),
        ("intent_hash", "other-intent"),
        ("policy_binding", "other-policy"),
        ("actor_id", "mallory"),
        ("target_type", "profile"),
        ("target_id", "999"),
    ],
)
def test_every_reconciliation_lineage_field_must_match_canonical_first_effect(
    tmp_path: Path,
    field: str,
    bad_value: object,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    raw = _effect_record(effect_id="fx-lineage", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    reconciliations.append_durable(
        _reconciliation(effect=raw, **{field: bad_value})
    )

    with pytest.raises(RecoveryProjectorCorruptError, match=field):
        RecoveryProjector(effects, reconciliations).project()


def test_missing_reconciliation_file_is_valid_empty_and_keeps_unknown_blocked(
    tmp_path: Path,
) -> None:
    effects = EffectLedger(path=tmp_path / "effects.ndjson")
    raw = _effect_record(effect_id="fx-unknown", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    guard = RecoveryGuard(effects)

    status = guard.hydrate()
    block = guard.require_clear(raw.semantic_key, refresh=False)

    assert status.available is True
    assert block is not None
    assert block.effect_ids == ("fx-unknown",)
    assert guard.reconciliation_ledger.path == tmp_path / "reconciliations.ndjson"


def test_one_reconciled_effect_cannot_clear_another_same_key_unknown(
    tmp_path: Path,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    first = _effect_record(effect_id="fx-a", state=EffectState.EFFECT_UNKNOWN)
    second = _effect_record(effect_id="fx-b", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(first)
    effects.append_durable(second)
    reconciliations.append_durable(_reconciliation(effect=first))
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)

    guard.hydrate()
    block = guard.require_clear(first.semantic_key, refresh=False)

    assert block is not None
    assert block.effect_ids == ("fx-b",)

    reconciliations.append_durable(
        _reconciliation(
            effect=second,
            verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        )
    )
    assert guard.require_clear(first.semantic_key, refresh=True) is None


def test_locally_ambiguous_reconciliation_never_becomes_projection_authority(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    raw = _effect_record(effect_id="fx-ambiguous", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    reconciliation = _reconciliation(effect=raw)

    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected reconciliation fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    with pytest.raises(ReconciliationLedgerError, match="append failed"):
        reconciliations.append_durable(reconciliation)
    assert reconciliations.durability_ambiguous is True

    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    with pytest.raises(RecoveryGuardUnavailable):
        guard.hydrate()
    assert guard.status().available is False


def test_startup_reconciliation_redurability_failure_makes_guard_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    effects, reconciliations = _ledgers(tmp_path)
    raw = _effect_record(effect_id="fx-startup", state=EffectState.EFFECT_UNKNOWN)
    effects.append_durable(raw)
    reconciliations.append_durable(_reconciliation(effect=raw))

    real_fsync = os.fsync
    failed = False

    def fail_once(fd: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            raise OSError("injected startup re-durability failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_once)
    restarted_ledger = ReconciliationLedger(path=reconciliations.path)
    guard = RecoveryGuard(effects, reconciliation_ledger=restarted_ledger)

    with pytest.raises(RecoveryGuardUnavailable):
        guard.hydrate()
    assert guard.status().available is False


class _BlockingReconciliationLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.entered = threading.Event()
        self.release = threading.Event()

    def read_authoritative(self) -> list[ReconciliationRecord]:
        self.entered.set()
        assert self.release.wait(timeout=2.0)
        return super().read_authoritative()


class _ProbeReconciliationLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.entered = threading.Event()

    def read_authoritative(self) -> list[ReconciliationRecord]:
        self.entered.set()
        return super().read_authoritative()


def test_same_recovery_domain_guards_share_publication_serialization(
    tmp_path: Path,
) -> None:
    effect_path = tmp_path / "effects.ndjson"
    reconciliation_path = tmp_path / "reconciliations.ndjson"
    blocking = _BlockingReconciliationLedger(path=reconciliation_path)
    probe = _ProbeReconciliationLedger(path=reconciliation_path)
    first = RecoveryGuard(
        EffectLedger(path=effect_path),
        reconciliation_ledger=blocking,
    )
    second = RecoveryGuard(
        EffectLedger(path=effect_path),
        reconciliation_ledger=probe,
    )
    errors: list[BaseException] = []

    def refresh(guard: RecoveryGuard) -> None:
        try:
            guard.refresh()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    first_thread = threading.Thread(target=refresh, args=(first,))
    first_thread.start()
    assert blocking.entered.wait(timeout=2.0)

    second_thread = threading.Thread(target=refresh, args=(second,))
    second_thread.start()
    assert probe.entered.wait(timeout=0.05) is False

    blocking.release.set()
    first_thread.join(timeout=2.0)
    second_thread.join(timeout=2.0)

    assert errors == []
    assert first_thread.is_alive() is False
    assert second_thread.is_alive() is False
    assert probe.entered.is_set()
