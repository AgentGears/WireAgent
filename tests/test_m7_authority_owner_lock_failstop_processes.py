"""Fresh-process fail-stop regressions for M7 Layer-1 owner transitions."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from webwire.authority import AuthorityOwnerLock

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


def _probe(state_dir: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), "probe", str(state_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    stdout = completed.stdout.strip().splitlines()
    assert stdout, f"worker produced no JSON; stderr={completed.stderr!r}"
    payload = json.loads(stdout[-1])
    assert isinstance(payload, dict)
    return completed, payload


def test_hidden_open_interruption_fail_stops_real_process_and_allows_successor(
    tmp_path: Path,
) -> None:
    """An unpublishable raw fd is cleaned only by process death, never guessed at."""

    state_dir = tmp_path / "state"
    interrupted = subprocess.run(
        [sys.executable, str(_WORKER), "hidden-open-interrupt", str(state_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert interrupted.returncode == AuthorityOwnerLock._HIDDEN_OPEN_EXIT_CODE

    # The fail-stopped process cannot retain any descriptor or process-local
    # reservation. A genuinely fresh process must be able to establish ownership.
    successor, payload = _probe(state_dir)
    assert successor.returncode == 0
    assert payload["acquired"] is True
    assert payload["busy"] is False
