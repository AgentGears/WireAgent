"""M6 Layer-5 qualification using genuine fresh Python processes."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_WORKER = Path(__file__).with_name("_m6_restart_worker.py")


def _run_worker(
    state_dir: Path,
    mode: str,
    *args: str,
    expected_returncode: int = 0,
) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), mode, str(state_dir), *args],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert completed.returncode == expected_returncode, (
        f"worker {mode!r} returned {completed.returncode}; "
        f"stdout={completed.stdout!r} stderr={completed.stderr!r}"
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        return {}
    payload = json.loads(lines[-1])
    assert isinstance(payload, dict)
    return payload


def test_r2_reserved_is_unknown_and_blocked_after_true_process_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    _run_worker(state_dir, "create_reserved")

    restarted = _run_worker(state_dir, "probe_guard")

    assert restarted["available"] is True
    assert restarted["blocked"] is True
    assert restarted["effective_states"] == ["EFFECT_UNKNOWN"]
    assert restarted["reconciliation_count"] == 0
    assert restarted["pending_confirmation_count"] == 0


@pytest.mark.parametrize(
    "verdict",
    ["CONFIRMED_EFFECT", "CONFIRMED_NO_EFFECT"],
)
def test_r20_r34_durable_reconciliation_survives_process_crash_before_publication(
    tmp_path: Path,
    verdict: str,
) -> None:
    state_dir = tmp_path / verdict.lower()

    _run_worker(
        state_dir,
        "crash_after_durable_before_publish",
        verdict,
        expected_returncode=73,
    )
    restarted = _run_worker(state_dir, "probe_guard")

    assert restarted["available"] is True
    assert restarted["blocked"] is False
    assert restarted["reconciliation_count"] == 1
    assert restarted["pending_confirmation_count"] == 0


def test_r36_fresh_process_startup_redurabilizes_surviving_reconciliation(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "startup-redurable"
    _run_worker(state_dir, "create_reconciled", "CONFIRMED_EFFECT")

    restarted = _run_worker(state_dir, "probe_startup_fsync")

    assert restarted["available"] is True
    assert restarted["startup_redurability_called"] is True
    assert restarted["blocked"] is False


def test_r37_fresh_process_startup_redurability_failure_fails_closed(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "startup-fsync-failure"
    _run_worker(state_dir, "create_reconciled", "CONFIRMED_NO_EFFECT")

    restarted = _run_worker(state_dir, "probe_startup_fsync_failure")

    assert restarted["hydrated"] is True
    assert restarted["available"] is False
    assert "ReconciliationLedgerError" in str(restarted["error"])


def test_clean_no_row_failure_loses_committed_authority_across_restart(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "clean-no-row"

    failed_process = _run_worker(state_dir, "clean_failure_no_row")
    assert failed_process["ambiguous"] is False
    assert failed_process["authority_committed"] is True
    assert failed_process["confirmation_epoch"] == 1
    assert failed_process["row_exists"] is False

    blocked_after_restart = _run_worker(state_dir, "probe_guard")
    assert blocked_after_restart["available"] is True
    assert blocked_after_restart["blocked"] is True
    assert blocked_after_restart["reconciliation_count"] == 0
    assert blocked_after_restart["pending_confirmation_count"] == 0

    fresh_resolution = _run_worker(state_dir, "resolve_fresh_after_restart")
    assert fresh_resolution["fresh_confirmation_used"] is True
    assert fresh_resolution["resolved"] is True
    assert fresh_resolution["reconciliation_count"] == 1

    final_restart = _run_worker(state_dir, "probe_guard")
    assert final_restart["available"] is True
    assert final_restart["blocked"] is False
