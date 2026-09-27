"""M6 Layer-6 target-platform qualification for both safety ledgers.

The Windows-only tests in this module are evidence for the deliberately bounded
M6 claim: CPython's file-descriptor fsync/_commit path, exact-fact
re-durability, normalized same-path identity, and fresh-process recovery work on
the tested Windows runner. They do not claim a portable parent-directory fsync,
hardware-cache bypass, network-filesystem semantics, or cross-process writer
linearizability.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import (
    EffectLedger,
    EffectLedgerCorruptError,
    EffectLedgerRecord,
    EffectState,
    ReconciliationLedger,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)

_WORKER = Path(__file__).with_name("_m6_restart_worker.py")
_WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Windows qualification only")


def _effect(*, effect_id: str = "fx-l6-windows") -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=f"actor|post|post|{effect_id}|",
        state=EffectState.RESERVED,
        action_type="post",
        intent_hash=f"intent-{effect_id}",
        policy_binding="policy-l6-windows",
        actor_id="actor",
        target_type="post",
        target_id=effect_id,
        timestamp="2026-09-27T18:20:00+00:00",
    )


def _reconciliation(*, effect_id: str = "fx-l6-windows") -> ReconciliationRecord:
    evidence = {
        "basis": "layer6-windows-durability",
        "observed_at": "2026-09-27T18:21:00+00:00",
        "observations": [{"kind": "platform", "value": "qualified"}],
    }
    return ReconciliationRecord(
        reconciliation_id="rec-l6-windows",
        effect_id=effect_id,
        semantic_key=f"actor|post|post|{effect_id}|",
        action_type="post",
        intent_hash=f"intent-{effect_id}",
        policy_binding="policy-l6-windows",
        actor_id="actor",
        target_type="post",
        target_id=effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        operator_id="local-admin",
        evidence_hash=canonical_evidence_hash(evidence),
        evidence=evidence,
        timestamp="2026-09-27T18:22:00+00:00",
    )


def _run_restart_worker(
    state_dir: Path,
    mode: str,
    *args: str,
) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), mode, str(state_dir), *args],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, (
        f"worker {mode!r} returned {completed.returncode}; "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert lines, f"worker {mode!r} produced no JSON payload"
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


def test_effect_ledger_rejects_complete_json_without_terminal_newline(
    tmp_path: Path,
) -> None:
    path = tmp_path / "effects.ndjson"
    path.write_text(_effect().to_jsonl(), encoding="utf-8")

    with pytest.raises(EffectLedgerCorruptError, match="torn tail"):
        EffectLedger(path=path).read_records()


def test_effect_ledger_rejects_non_utf8_as_corruption(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    path.write_bytes(b"\xff\n")

    with pytest.raises(EffectLedgerCorruptError, match="valid UTF-8"):
        EffectLedger(path=path).read_records()


def test_effect_ledger_path_registry_uses_normcase_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # POSIX normcase is deliberately a no-op. Simulate the case folding used by
    # Windows so this portability invariant remains covered on every CI runner.
    monkeypatch.setattr(os.path, "normcase", lambda value: value.casefold())
    upper = EffectLedger(path=tmp_path / "CaseDomain" / "effects.ndjson")
    lower = EffectLedger(path=tmp_path / "casedomain" / "effects.ndjson")

    assert upper._lock is lower._lock


@_WINDOWS_ONLY
def test_windows_case_variant_effect_paths_share_one_writer_lock(tmp_path: Path) -> None:
    upper = EffectLedger(path=tmp_path / "CaseDomain" / "Effects.ndjson")
    lower = EffectLedger(path=tmp_path / "casedomain" / "effects.ndjson")

    assert os.path.normcase(str(upper.path.resolve(strict=False))) == os.path.normcase(
        str(lower.path.resolve(strict=False))
    )
    assert upper._lock is lower._lock


@_WINDOWS_ONLY
def test_windows_both_ledgers_create_nested_paths_and_accept_real_fsync(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "nested" / "wireagent" / "state"
    cfg = WebWireConfig(state_dir=state_dir, kill_env_var=None)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())

    effect = _effect()
    reconciliation = _reconciliation()
    effects.append_durable(effect)
    reconciliations.append_durable(reconciliation)

    assert effects.read_records() == [effect]
    assert reconciliations.read_authoritative() == [reconciliation]
    assert effects.path.exists()
    assert reconciliations.path.exists()

    # Exercise CPython's actual Windows fsync path on the same writable access
    # mode used for exact-fact/startup re-durability. No monkeypatch is involved.
    for path in (effects.path, reconciliations.path):
        fd = os.open(path, os.O_RDWR)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


@_WINDOWS_ONLY
def test_windows_parent_directory_hook_is_explicitly_noop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    opened: list[object] = []
    real_open = os.open

    def track_open(path, flags, *args):  # type: ignore[no-untyped-def]
        opened.append(path)
        return real_open(path, flags, *args)

    monkeypatch.setattr(os, "open", track_open)
    EffectLedger._fsync_directory(tmp_path)
    ReconciliationLedger._fsync_directory(tmp_path)

    # The Layer-6 claim ceiling is intentional: these helpers do not pretend a
    # portable Python directory fsync occurred on Windows.
    assert opened == []


@_WINDOWS_ONLY
def test_windows_durable_reconciliation_survives_fresh_process_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "restart"
    created = _run_restart_worker(
        state_dir,
        "create_reconciled",
        "CONFIRMED_NO_EFFECT",
    )
    restarted = _run_restart_worker(state_dir, "probe_guard")

    assert created["resolved"] is True
    assert created["reconciliations"] == 1
    assert restarted["available"] is True
    assert restarted["blocked"] is False
    assert restarted["reconciliation_count"] == 1
    assert restarted["pending_confirmation_count"] == 0


@_WINDOWS_ONLY
def test_windows_platform_fingerprint_is_real_target_platform() -> None:
    # Keep the assertion intentionally narrow. The CI log records exact runner
    # details separately; this prevents accidental execution on a POSIX runner
    # from being counted as R46 evidence.
    assert os.name == "nt"
    assert sys.platform == "win32"
    assert platform.system() == "Windows"
