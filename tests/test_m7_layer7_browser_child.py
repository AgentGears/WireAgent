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

Execution oracle (F-118): "the successor SERVES a production request
while the orphan lives" is proven by the Dispatcher invocation log —
exactly ONE preview invoke reached the successor's real dispatcher —
plus a REAL minted confirmation token in the response (a structure only
the successor's live confirmation machinery can produce), not merely
the presence of an ``ok`` field (a well-formed pre-admission rejection
also carries ``ok``; the negative control below demonstrates that
difference). Liveness is zombie-rejecting (F-115) and is re-verified
while the successor runs (F-117 discipline).
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


def _invoke_count(events_path: Path) -> int:
    if not events_path.exists():
        return 0
    return len([line for line in events_path.read_text(encoding="utf-8").splitlines() if '"invoke"' in line])


def _preview_request(scratch: Path, record: dict, text: str, *, instance_override: str = None):  # type: ignore[assignment]
    """One REAL post_text preview over the production transport.
    ``instance_override`` (the negative control) addresses a DEAD owner
    instance — the request is rejected pre-admission with a
    well-formed envelope. The preview response carries the minted
    confirmation token in a distinctive structure — the token is
    produced only by the successor's real confirmation machinery, so
    its presence is execution evidence, not envelope shape."""
    env = scratch / f"preview-{secrets.token_hex(4)}.json"
    env.write_text(json.dumps({"text": text}), encoding="utf-8")
    client = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        instance_override if instance_override is not None else record["instance_id"],
        "post_text",
        env,
        secrets.token_hex(16),
        "normal",
    )
    return client.result()["response"]


def _child_readiness(state_dir: Path, pid: int) -> dict:
    """Wait for and return one child's OWN readiness record — the
    per-child marker ``browser-child-<pid>.pid`` whose content carries
    the child's self-reported pid AND kernel start time (F-124). The
    marker's existence proves the child interpreter reached its holding
    loop, not merely that a process was spawned."""

    marker = state_dir / f"browser-child-{pid}.pid"
    deadline = time.monotonic() + 10
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return json.loads(marker.read_text(encoding="utf-8"))


def test_T33_owner_crash_leaves_orphan_child_successor_starts_own_runtime(tmp_path: Path) -> None:
    """T33: a full production owner whose SessionManager.start() is the
    UNTOUCHED production method — the attach-gate and owned-launch
    decision run as production code (F-114); only the SuperBrowser
    dependency is a controlled subprocess-backed stand-in injected at
    its module boundary. The production launch path starts a REAL OS
    child; the owner dies uncleanly without stopping it; the child
    SURVIVES as a genuinely-executing orphan (its OWN readiness record,
    matched to pid and kernel start time, plus zombie-rejecting
    liveness, F-115/F-124). The successor's production start() selects
    owned-launch again, starts its OWN child — whose OWN readiness
    record and start-time identity are verified (F-124) — and EXECUTES
    a production request while the orphan still lives: proven by the
    invocation log (exactly one preview invoke reached the successor's
    Dispatcher) and a real minted confirmation token (F-118). The
    orphan is never adopted, never blocks takeover, and carries no
    production authority."""
    scratch = _scratch(tmp_path, "t33")
    state_dir = scratch / "state"
    child_gate = harness.gate(scratch, "child")
    owner = successor = None
    orphan_pid = orphan_starttime = None
    successor_child_pid = successor_child_starttime = None

    try:
        events = scratch / "events.ndjson"
        owner = harness.start_worker(
            scratch,
            "owner-browser-child",
            state_dir,
            harness.gate(scratch, "die"),
            child_gate,
            "die",
            events,
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

        # F-115/F-124: child-produced readiness — the CHILD ITSELF wrote
        # its per-pid marker (pid + starttime), and the orphan is proven
        # executing (not zombie) BEFORE the owner dies.
        orphan_readiness = _child_readiness(state_dir, orphan_pid)
        assert orphan_readiness["pid"] == orphan_pid, (
            "the child itself produced its readiness record"
        )
        orphan_starttime = orphan_readiness.get("starttime")
        assert harness.pid_alive(orphan_pid), "the browser child is executing before the owner dies"
        if orphan_starttime is not None:
            assert harness.proc_starttime(orphan_pid) == orphan_starttime

        # The owner dies uncleanly WITHOUT stopping its child: the child
        # survives exactly like a launched browser would.
        harness.open_gate(scratch, "die")
        deadline = time.monotonic() + 15
        while owner.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert owner.poll() == harness.EXIT_DIED_UNCLEAN, "the owner died uncleanly"

        # THE SURVIVAL OBSERVATION: the orphan child is really still
        # alive and executing — present, not a zombie, identity-tied.
        deadline = time.monotonic() + 10
        while not harness.pid_alive(orphan_pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert harness.pid_alive(orphan_pid), "the browser child must survive the owner's unclean death"
        if orphan_starttime is not None:
            assert harness.proc_starttime(orphan_pid) == orphan_starttime

        # The successor: production start() runs again — owned-launch,
        # its OWN child — while the orphan lives. The orphan never
        # blocks takeover and is never adopted.
        successor_events = scratch / "successor-events.ndjson"
        successor = harness.start_worker(
            scratch,
            "owner-browser-child",
            state_dir,
            harness.gate(scratch, "done"),
            child_gate,
            "clean",
            successor_events,
        )
        successor_record = successor.result()
        assert successor_record["started"] is True, successor_record
        assert successor_record["instance_id"] != record["instance_id"], "a fresh authority instance"
        assert successor_record["ownership_mode"] == "owned", (
            "the successor's PRODUCTION start() selected owned-launch; it did not attach"
        )
        successor_child_pid = successor_record["browser_child_pid"]
        assert successor_child_pid != orphan_pid, (
            "the successor must start its OWN child, never attach to the orphan"
        )

        # F-124: the SUCCESSOR's own child reached its readiness loop —
        # its OWN per-pid marker, matched to pid and kernel start time —
        # before the child-start claim is accepted.
        successor_readiness = _child_readiness(state_dir, successor_child_pid)
        assert successor_readiness["pid"] == successor_child_pid
        successor_child_starttime = successor_readiness.get("starttime")
        if successor_child_starttime is not None:
            assert harness.proc_starttime(successor_child_pid) == successor_child_starttime, (
                "the successor's child is the same process its readiness record describes"
            )

        # Both children are alive and executing simultaneously: the
        # orphan was never adopted, and the successor's runtime is its
        # own fresh process.
        assert harness.pid_alive(orphan_pid), "the orphan is untouched by the takeover"
        assert harness.pid_alive(successor_child_pid), "the successor's own child is executing"

        # F-118 EXECUTION ORACLE: the request must reach the successor's
        # real Dispatcher (exactly one invoke) AND return a REAL minted
        # confirmation token — a structure only the successor's live
        # confirmation machinery can produce. A well-formed rejection
        # alone does not qualify as service (the negative control below
        # demonstrates the difference).
        response = _preview_request(scratch, successor_record, "t33 successor serves while orphan lives")
        assert isinstance(response, dict), response
        token = (response.get("data") or {}).get("data", {}).get("confirmation_token")
        assert token, f"a real confirmation token was minted by the successor: {response}"
        assert _invoke_count(successor_events) == 1, (
            "exactly one preview invocation reached the successor's real Dispatcher"
        )
        assert harness.pid_alive(orphan_pid), "the orphan still carries no production authority"
    finally:
        # Exception-safe cleanup (F-120/F-126): each gate and process is
        # handled independently, with identity-aware last-resort kills.
        for gate_name in ("die", "done", "child"):
            harness.safe_open_gate(scratch, gate_name)
        for handle in (owner, successor):
            if handle is not None and handle.poll() is None:
                try:
                    handle.kill()
                except OSError:
                    pass
        for pid, starttime in (
            (orphan_pid, orphan_starttime),
            (successor_child_pid, successor_child_starttime),
        ):
            if pid is None:
                continue
            deadline = time.monotonic() + 10
            while harness.pid_alive(pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            harness.kill_pid_if_same_process(pid, starttime)
        for handle in (owner, successor):
            if handle is not None:
                try:
                    handle.wait(timeout=30)
                except Exception:  # noqa: BLE001 - cleanup must never mask the failure
                    pass


def test_T33_negative_control_unadmitted_request_does_not_count_as_service(tmp_path: Path) -> None:
    """F-118's adversarial acceptance test: force a PRE-ADMISSION IPC
    rejection whose response envelope is well-formed (a stale
    authority-instance post_text preview). The OLD oracle — "a dict with
    an ok field" — accepts this response, which is exactly the
    false-positive path; the EXECUTION oracle (invoke count plus a real
    minted confirmation token) must reject it. This control proves the
    service assertion in the T33 test above cannot pass on an
    unadmitted request."""
    scratch = _scratch(tmp_path, "t33ctl")
    state_dir = scratch / "state"
    child_gate = harness.gate(scratch, "child")
    owner = None
    orphan_pid = None

    try:
        events = scratch / "events.ndjson"
        owner = harness.start_worker(
            scratch,
            "owner-browser-child",
            state_dir,
            harness.gate(scratch, "stop"),
            child_gate,
            "clean",
            events,
        )
        record = owner.result()
        assert record["started"] is True, record
        orphan_pid = record["browser_child_pid"]

        # The pre-admission fault: the envelope addresses a DEAD owner
        # instance. The response is well-formed... but the request was
        # never admitted.
        response = _preview_request(scratch, record, "t33 negative control", instance_override="0" * 32)

        # The FALSE-POSITIVE PATH, demonstrated: the old oracle accepts.
        assert isinstance(response, dict) and "ok" in response, (
            f"the old oracle would have accepted this rejection: {response}"
        )
        assert response.get("ok") is False, response

        # The EXECUTION oracle rejects: nothing reached the Dispatcher,
        # and no confirmation token was minted.
        assert _invoke_count(events) == 0, "the stale-instance request was rejected BEFORE admission"
        assert not (response.get("data") or {}).get("data", {}).get("confirmation_token"), (
            "a pre-admission rejection mints no confirmation token"
        )
    finally:
        harness.safe_open_gate(scratch, "stop")
        harness.safe_open_gate(scratch, "child")
        if owner is not None:
            if owner.poll() is None:
                try:
                    owner.kill()
                except OSError:
                    pass
            try:
                owner.wait(timeout=30)
            except Exception:  # noqa: BLE001 - cleanup must never mask the failure
                pass
        if orphan_pid is not None:
            deadline = time.monotonic() + 10
            while harness.pid_alive(orphan_pid) and time.monotonic() < deadline:
                time.sleep(0.1)
            harness.kill_pid_if_same_process(orphan_pid, None)
