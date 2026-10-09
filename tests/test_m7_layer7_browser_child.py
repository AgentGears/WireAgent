"""M7 Layer 7 round two, tranche two — T33: crash with a SURVIVING
browser child, with real child-process evidence.

Frozen law (M7 §17.6 / invariant 982): the child browser is not
authority. When an owner crash leaves the browser child alive, the
successor does NOT attach to the orphan as production session — it
starts its OWN runtime under its own ownership — and any already-
emitted remote effect is represented only through M5/M6 history and
evidence.

Substrate honesty: the harness owner's SessionManager launches a REAL
OS child process (a fresh, standing interpreter holding state — real
surviving-child evidence at the process boundary where the ownership
law lives) in place of a launched browser binary. The ambient-attach
refusal half of the law (a successor must not CDP-attach to a running
browser by default) is production code already qualified in
tests/test_session_persistence.py::test_attach_refused_without_allow_attach;
this tranche owns the process-boundary half.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _pid_alive(pid: int) -> bool:
    """Real OS observation of a child process's life, cross-platform."""

    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(  # noqa: S606
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                ok = ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))  # noqa: S606
                return bool(ok) and code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # noqa: S606
        import os as _os

        _os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError on POSIX: the process EXISTS but belongs to
        # another uid — still alive for survival purposes.
        return sys.platform != "win32" and isinstance(sys.exc_info()[1], PermissionError)


def test_T33_owner_crash_leaves_orphan_child_successor_starts_own_runtime(tmp_path: Path) -> None:
    """T33: a full production owner whose runtime is a REAL child
    process dies uncleanly; the child SURVIVES as an orphan (observed
    by pid at the OS boundary). The successor acquires the domain while
    the orphan lives, hydrates durable truth, and reaches READY with
    its OWN newly-launched child — a different pid, never the orphan's.
    The orphan is never adopted, never blocks takeover, and carries no
    production authority: the successor serves a real request through
    its own runtime while the orphan is still alive."""
    scratch = _scratch(tmp_path, "t33")
    state_dir = scratch / "state"
    child_gate = harness.gate(scratch, "child")

    owner = harness.start_worker(
        scratch, "owner-browser-child", state_dir, harness.gate(scratch, "die"), child_gate, "die"
    )
    record = owner.result()
    assert record["started"] is True, record
    assert record["ownership_mode"] == "owned", "production default: the owner owns its browser child"
    orphan_pid = record["browser_child_pid"]

    # The owner dies uncleanly WITHOUT stopping its child: the child
    # survives exactly like a launched browser would.
    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN, "the owner died uncleanly"

    # THE SURVIVAL OBSERVATION: the orphan child is really still alive.
    deadline = time.monotonic() + 10
    while not _pid_alive(orphan_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _pid_alive(orphan_pid), "the browser child must survive the owner's unclean death"

    # The successor: acquires while the orphan lives, hydrates, starts
    # its OWN child, reaches READY. The orphan never blocks takeover.
    successor = harness.start_worker(
        scratch, "owner-browser-child", state_dir, harness.gate(scratch, "done"), child_gate, "clean"
    )
    successor_record = successor.result()
    try:
        assert successor_record["started"] is True, successor_record
        assert successor_record["instance_id"] != record["instance_id"], "a fresh authority instance"
        assert successor_record["ownership_mode"] == "owned", (
            "the successor OWNS a fresh child; it does not attach to the orphan"
        )
        successor_child_pid = successor_record["browser_child_pid"]
        assert successor_child_pid != orphan_pid, (
            "the successor must start its OWN child, never attach to the orphan"
        )

        # Both children are alive simultaneously: the orphan was never
        # adopted (it would be indistinguishable from takeover only if
        # the successor had bound it), and the successor's runtime is
        # its own fresh process.
        assert _pid_alive(orphan_pid), "the orphan is untouched by the takeover"
        assert _pid_alive(successor_child_pid), "the successor's own child is live"

        # The successor is a working production owner over the real
        # transport WHILE the orphan still lives: mutation authority
        # flows through the successor's runtime, not the orphan.
        env = scratch / "health-payload.json"
        env.write_text(json.dumps({}), encoding="utf-8")
        client = harness.start_worker(
            scratch,
            "ipc-request",
            Path(successor_record["endpoint"]),
            successor_record["build_id"],
            successor_record["instance_id"],
            "health",
            env,
            secrets.token_hex(16),
            "normal",
        )
        response = client.result()["response"]
        # T21 precedent: with the stub DOM surface the health operation
        # returns a well-formed ok:false diagnostic — "serving" means the
        # successor's transport+dispatch executed the operation, not that
        # the stub browser reports green.
        assert isinstance(response, dict) and "ok" in response, (
            f"the successor serves production requests while the orphan lives: {response}"
        )
        assert _pid_alive(orphan_pid), "the orphan still carries no production authority"
    finally:
        harness.open_gate(scratch, "done")
        successor.wait(timeout=30)
        # Orphan cleanup: the dead owner can never stop it; the CHILD
        # GATE releases its self-termination loop, then the controller
        # makes sure (kill by pid if the graceful exit lost the race).
        harness.open_gate(scratch, "child")
        deadline = time.monotonic() + 10
        while _pid_alive(orphan_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if _pid_alive(orphan_pid):
            import os

            try:
                os.kill(orphan_pid, 9)
            except OSError:
                pass
        owner.wait(timeout=30)
