"""M7 Layer 7 round two, tranche two — T33: crash with a SURVIVING
browser child, with real child-process evidence.

Frozen law (M7 §17.6 / invariant 982): the child browser is not
authority. When an owner crash leaves the browser child alive, the
successor does NOT attach to the orphan as production session — it
starts its OWN runtime under its own ownership — and any already-
emitted remote effect is represented only through M5/M6 history and
evidence.

Substrate honesty (F-114): PRODUCTION SessionManager.start() runs
unmodified — the attach-gate and owned-launch decision are the code
under qualification. Only the SuperBrowser dependency is substituted at
its module boundary (``webwire.session.SuperBrowser``) with a
controlled adapter whose start() launches a REAL OS child process (a
fresh, standing interpreter holding state — real surviving-child
evidence at the process boundary where the ownership law lives) and
whose stop() terminates it. Browser-BINARY platform mechanics remain
Layer 8's qualification. The ambient-attach refusal half of the law (a
successor must not CDP-attach to a running browser by default) is
production code already qualified in
tests/test_session_persistence.py::test_attach_refused_without_allow_attach;
this tranche owns the process-boundary half: production startup always
selects owned-launch, even with a live orphan present.
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
    """Real OS observation of a child process's life, cross-platform —
    and ZOMBIE-REJECTING (F-115): a PID that exists only as an unreaped
    corpse is NOT a surviving child. On /proc platforms the process
    state is read directly (state Z fails); elsewhere os.kill(pid, 0)
    presence is the fallback."""

    if pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            # Field 3 is the state letter; the comm field (2) may contain
            # spaces/parens, so parse AFTER the last ')'.
            raw = stat_path.read_text(encoding="utf-8")
            state = raw[raw.rindex(")") + 1 :].split()[0]
            return state not in {"Z", "X", "x"}
        except (OSError, ValueError, IndexError):
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
    """T33: a full production owner whose SessionManager.start() is the
    UNTOUCHED production method — the attach-gate and owned-launch
    decision run as production code (F-114); only the SuperBrowser
    dependency is a controlled subprocess-backed stand-in injected at
    its module boundary. The production launch path starts a REAL OS
    child; the owner dies uncleanly without stopping it; the child
    SURVIVES as a genuinely-executing orphan (child-produced readiness
    record + zombie-rejecting liveness, F-115). The successor's
    production start() selects owned-launch again, starts its OWN child
    (a different pid, never the orphan's), and serves a production
    request over the real transport while the orphan still lives. The
    orphan is never adopted, never blocks takeover, and carries no
    production authority."""
    scratch = _scratch(tmp_path, "t33")
    state_dir = scratch / "state"
    child_gate = harness.gate(scratch, "child")

    owner = harness.start_worker(
        scratch, "owner-browser-child", state_dir, harness.gate(scratch, "die"), child_gate, "die"
    )
    record = owner.result()
    assert record["started"] is True, record
    assert record["ownership_mode"] == "owned", (
        "the PRODUCTION start() decision selected owned-launch (the attach gate + mode config)"
    )
    assert record["session_state"] == "no_file", (
        "production _restore_session ran through the real start() path"
    )
    orphan_pid = record["browser_child_pid"]

    # F-115: child-produced readiness — the CHILD ITSELF wrote its pid
    # record; the orphan is proven executing (not zombie) BEFORE the
    # owner dies.
    readiness = state_dir / "browser-child.pid"
    deadline = time.monotonic() + 10
    while not readiness.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert readiness.read_text(encoding="utf-8").strip() == str(orphan_pid), (
        "the child itself produced its readiness record"
    )
    assert _pid_alive(orphan_pid), "the browser child is executing before the owner dies"

    # The owner dies uncleanly WITHOUT stopping its child: the child
    # survives exactly like a launched browser would.
    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN, "the owner died uncleanly"

    # THE SURVIVAL OBSERVATION: the orphan child is really still alive
    # and executing — present AND not a zombie (F-115).
    deadline = time.monotonic() + 10
    while not _pid_alive(orphan_pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _pid_alive(orphan_pid), "the browser child must survive the owner's unclean death"

    # The successor: production start() runs again — owned-launch, its
    # OWN child — while the orphan lives. The orphan never blocks
    # takeover and is never adopted.
    successor = harness.start_worker(
        scratch, "owner-browser-child", state_dir, harness.gate(scratch, "done"), child_gate, "clean"
    )
    successor_record = successor.result()
    try:
        assert successor_record["started"] is True, successor_record
        assert successor_record["instance_id"] != record["instance_id"], "a fresh authority instance"
        assert successor_record["ownership_mode"] == "owned", (
            "the successor's PRODUCTION start() selected owned-launch; it did not attach"
        )
        successor_child_pid = successor_record["browser_child_pid"]
        assert successor_child_pid != orphan_pid, (
            "the successor must start its OWN child, never attach to the orphan"
        )

        # Both children are alive and executing simultaneously: the
        # orphan was never adopted, and the successor's runtime is its
        # own fresh process.
        assert _pid_alive(orphan_pid), "the orphan is untouched by the takeover"
        assert _pid_alive(successor_child_pid), "the successor's own child is executing"

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
