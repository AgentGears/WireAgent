"""M7 Layer 7 — qualification-harness safety controls (F-128's
acceptance tests).

The identity-aware last-resort termination helper must FAIL CLOSED:
missing identity evidence (an unrecorded kernel start time, an
unreadable /proc record, or a start time that does not match the live
process) must PREVENT the termination — never permit it on presence
alone. These controls simulate exactly the recorded-pid-without-
identity situations against a LIVE process (this test process) and
verify no termination call occurs.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402


def test_kill_helper_fails_closed_without_recorded_identity() -> None:
    """F-128 acceptance control 1: a LIVE process occupies the pid, but
    the recorded start time is unknown — the exact state a test reaches
    when a child's readiness marker was never retrieved. The helper
    must refuse to terminate: no identity, no kill."""
    live_pid = os.getpid()
    assert harness.pid_alive(live_pid)
    assert harness.kill_pid_if_same_process(live_pid, None) is False
    assert harness.pid_alive(live_pid), "no termination may occur without recorded identity"


def test_kill_helper_fails_closed_on_identity_mismatch() -> None:
    """F-128 acceptance control 2: a LIVE process occupies the pid, and
    a start time IS recorded — but it is not the live process's start
    time (the pid was reused, or the record is wrong). The helper must
    refuse: a different process owns the pid now."""
    live_pid = os.getpid()
    recorded_but_different = -1  # no kernel start time is negative
    assert harness.kill_pid_if_same_process(live_pid, recorded_but_different) is False
    assert harness.pid_alive(live_pid), "a start-time mismatch must never terminate the live pid"


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="positive path needs /proc identity")
def test_kill_helper_terminates_only_under_matching_identity() -> None:
    """F-128 positive path: WITH a matching /proc start-time identity
    the helper does terminate — the fail-closed gate is not a
    refusal to ever act, but a refusal to act without proof."""
    import subprocess

    child = subprocess.Popen(  # noqa: S603 - fixed interpreter + inline sleep
        [sys.executable, "-c", "import time; time.sleep(30)"],
    )
    try:
        starttime = harness.proc_starttime(child.pid)
        assert starttime is not None, "the spawned child must expose /proc identity"
        assert harness.kill_pid_if_same_process(child.pid, starttime) is True
        deadline = time.monotonic() + 10
        while harness.pid_alive(child.pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert harness.pid_alive(child.pid) is False, "the identity-matched kill took effect"
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)
