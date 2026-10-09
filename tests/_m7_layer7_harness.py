"""M7 Layer 7 — multi-process qualification harness (controller side).

Spawns REAL worker processes (real AuthorityOwnerLock, real
AuthoritySession, real Dispatcher/IPC where the scenario requires) and
observes only process/OS boundaries: exit status, JSON records the
worker writes, rendezvous/endpoint state on disk, and durable M5/M6
rows. Synchronization gate files under a per-test scratch directory are
TEST ORCHESTRATION, never authority signals.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

WORKER = Path(__file__).with_name("_m7_layer7_worker.py")

# Stable exit codes the worker contract defines (distinct from signal
# deaths, which the OS reports as negative returncodes on POSIX).
EXIT_OK = 0
EXIT_BUSY = 23
EXIT_DIED_UNCLEAN = 9  # os._exit code the owner-die scenarios use


def pid_alive(pid: int) -> bool:
    """Zombie-rejecting OS observation that a process is alive (F-115/
    F-117): on /proc platforms the process STATE is read directly — a
    PID that exists only as an unreaped corpse (state Z) or a dead/x
    corpse is NOT alive; on Windows the exit-code probe decides; on
    other POSIX the kill(0) presence check is the fallback."""

    if pid <= 0:
        return False
    stat_path = Path(f"/proc/{pid}/stat")
    if stat_path.exists():
        try:
            raw = stat_path.read_text(encoding="utf-8")
            # Field 3 is the state letter; comm (field 2) may contain
            # spaces/parens, so parse AFTER the last ')'.
            state = raw[raw.rindex(")") + 1 :].split()[0]
            return state not in {"Z", "X", "x"}
        except (OSError, ValueError, IndexError):
            return False
    try:
        import sys

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
        import os

        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError, PermissionError):
        # PermissionError on POSIX: the process EXISTS but belongs to
        # another uid — still alive for survival purposes.
        import sys

        return sys.platform != "win32" and isinstance(sys.exc_info()[1], PermissionError)


def proc_starttime(pid: int):
    """The kernel start time (field 22 of /proc/<pid>/stat, clock ticks)
    — a stable process-IDENTITY tie for a PID across the whole
    qualification window (F-117): the same pid with a different
    starttime is a REUSED pid, not the same process. None where /proc
    does not expose the stat file."""

    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        raw = stat_path.read_text(encoding="utf-8")
        fields = raw[raw.rindex(")") + 1 :].split()
        return int(fields[19])  # field 22 overall; field 1 = pid, 2 = comm
    except (OSError, ValueError, IndexError):
        return None


class WorkerHandle:
    """One running worker process plus its result plumbing."""

    def __init__(self, process: subprocess.Popen[Any], result_path: Path) -> None:
        self.process = process
        self.result_path = result_path

    @property
    def returncode(self) -> Optional[int]:
        return self.process.returncode

    def wait(self, timeout: float = 30.0) -> int:
        try:
            return self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=10)
            raise

    def terminate(self) -> None:
        """OS-level termination (SIGTERM/ TerminateProcess)."""
        self.process.terminate()

    def kill(self) -> None:
        """Hard kill (SIGKILL/ TerminateProcess)."""
        self.process.kill()

    def poll(self) -> Optional[int]:
        return self.process.poll()

    def result(self, timeout: float = 15.0) -> dict[str, Any]:
        """The worker's JSON record (written after its first stable
        observation, before any gate waits)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.result_path.exists():
                text = self.result_path.read_text(encoding="utf-8").strip()
                if text:
                    return json.loads(text)
            if self.poll() is not None and not self.result_path.exists():
                break
            time.sleep(0.02)
        raise TimeoutError(
            f"worker result {self.result_path} never appeared "
            f"(exit={self.poll()}, stdout tail follows)\n"
            f"{getattr(self.process, 'stdout', None) and _tail(self.process)}"
        )


def _tail(process: subprocess.Popen[Any]) -> str:
    return ""


def start_worker(
    scratch: Path,
    scenario: str,
    *args: str,
) -> WorkerHandle:
    """Launch one worker scenario. Result files and gates live under
    ``scratch``; each call mints a unique result name."""
    scratch.mkdir(parents=True, exist_ok=True)
    tag = f"{scenario}-{time.monotonic_ns()}"
    result_path = scratch / f"{tag}.json"
    argv = [sys.executable, str(WORKER), scenario, str(result_path), *map(str, args)]
    process = subprocess.Popen(  # noqa: S603 - fixed interpreter + repo file
        argv,
        cwd=str(Path(__file__).resolve().parent.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return WorkerHandle(process, result_path)


def gate(scratch: Path, name: str) -> Path:
    """An orchestration gate file (created by the controller to release a
    waiting worker; never read by production code)."""
    return scratch / f"gate-{name}"


def open_gate(scratch: Path, name: str) -> None:
    gate(scratch, name).write_text("go", encoding="utf-8")


def safe_open_gate(scratch: Path, name: str) -> None:
    """Best-effort gate release that can never mask the active failure
    (F-126): one gate's cleanup error must not strand the others."""

    try:
        open_gate(scratch, name)
    except OSError:
        pass


def kill_pid_if_same_process(pid: int, starttime) -> bool:
    """Identity-aware last-resort kill (F-126/F-128), FAIL-CLOSED: a
    pid is terminated only when the recorded kernel start time is
    KNOWN and /proc still shows the SAME process. Missing identity
    evidence — no recorded start time, or no readable /proc record —
    PREVENTS the kill rather than permitting it; a reused pid must
    never be terminated. Where /proc identity is unavailable (Windows,
    non-procfs POSIX), this helper never kills: cleanup relies on
    gates, worker handles, and the child's own self-termination
    timeout instead."""

    if pid <= 0 or starttime is None:
        return False
    current = proc_starttime(pid)
    if current is None or current != starttime:
        return False
    import os

    try:
        os.kill(pid, 9)
        return True
    except OSError:
        return False


def proc_state(pid: int):
    """The state letter (field 3 of /proc/<pid>/stat) — used to
    distinguish an identity persisting as an unreaped TERMINAL corpse
    (Z/X/x) from one still executing (F-129). None where /proc does
    not expose the record."""

    stat_path = Path(f"/proc/{pid}/stat")
    if not stat_path.exists():
        return None
    try:
        raw = stat_path.read_text(encoding="utf-8")
        return raw[raw.rindex(")") + 1 :].split()[0]
    except (OSError, ValueError, IndexError):
        return None


def wait_exit(handle: WorkerHandle, timeout: float = 30.0) -> int:
    return handle.wait(timeout=timeout)


def wait_record(path: Path, timeout: float = 15.0) -> dict[str, Any]:
    """Wait for a JSON file another process wrote (worker result or
    client record)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return json.loads(text)
        time.sleep(0.02)
    raise TimeoutError(f"record {path} never appeared within {timeout}s")


def stdout_records(handle: WorkerHandle, timeout: float = 20.0) -> list[dict[str, Any]]:
    """Parse the worker's stdout as JSON lines (event stream)."""
    try:
        out, _err = handle.process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        handle.process.kill()
        raise
    records = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records
