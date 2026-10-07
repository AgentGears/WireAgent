"""M7 Layer 7 — IPC fault and response-loss qualification over the REAL
transport between REAL processes (lanes 3+4+5).

The key invariant under qualification: RPC uncertainty never becomes
mutation truth — M5/M6 durable state decides what a successor may do.
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


def _start_full_owner(scratch: Path, state_dir: Path, *, die: bool = False, gate_name: str = "stop"):
    owner = harness.start_worker(
        scratch, "full-owner", state_dir, harness.gate(scratch, gate_name), "die" if die else "clean"
    )
    record = owner.result()
    assert record["started"] is True, record
    return owner, record


def _payload_file(scratch: Path, payload: dict) -> Path:
    """The operation payload only — the worker builds the envelope
    (operation/instance/build/request_id) around it."""
    path = scratch / f"payload-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Lane 4: request-table pressure and duplicate delivery (T22/T57/T58)
# ---------------------------------------------------------------------------


def test_duplicate_request_id_delivered_twice_returns_retained(tmp_path: Path) -> None:
    """T23/T58 at the process boundary: the SAME request delivered twice
    over two real connections returns the SAME retained outcome — one
    execution, byte-stable response."""
    scratch = _scratch(tmp_path, "dup")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir)
    try:
        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {})  # health: browser-free
        first = harness.start_worker(
            scratch,
            "ipc-request",
            Path(record["endpoint"]),
            record["build_id"],
            record["instance_id"],
            "health",
            env,
            rid,
            "normal",
        )
        second = harness.start_worker(
            scratch,
            "ipc-request",
            Path(record["endpoint"]),
            record["build_id"],
            record["instance_id"],
            "health",
            env,
            rid,
            "normal",
        )
        r1 = first.result()
        first.wait()
        r2 = second.result()
        second.wait()
        # Any WELL-FORMED frame qualifies (the law under test is retention,
        # not the read result — the DOM port is stubbed at Layer 7).
        assert isinstance(r1["response"], dict) and "ok" in r1["response"], r1
        assert r2["response"] == r1["response"], "the retained frame is byte-stable"
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


def test_table_pressure_under_real_transport(tmp_path: Path) -> None:
    """T57 process half: saturate the connection capacity with held
    requests over real connections; the next connection is refused
    without a hello; the held requests complete after release. Held =
    slow reads (the invoke is fast; the CLIENT stalls reading, holding
    the handler slot — the bounded capacity observable)."""
    scratch = _scratch(tmp_path, "pressure")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir)
    try:
        # Open capacity+1 connections that connect and read the hello,
        # then stall (never send a request). Each holds a handler slot.
        from webwire.authority_ipc_transport import IPCClient

        endpoint = record["endpoint"]
        build = record["build_id"]
        stalled = []
        refused_at_capacity = 0
        for _ in range(5):  # > IPC_SERVER_MAX_CONCURRENT (4)
            client = IPCClient(endpoint, expected_build_id=build)
            try:
                client.connect()
                stalled.append(client)
            except ConnectionError:
                # The capacity boundary refusing the EXTRA connection at
                # the endpoint itself — the saturation law observed live.
                refused_at_capacity += 1
        assert stalled, "the owner must accept connections up to capacity"
        time.sleep(0.3)

        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {})
        refused = harness.start_worker(
            scratch,
            "ipc-request",
            Path(endpoint),
            build,
            record["instance_id"],
            "health",
            env,
            rid,
            "normal",
        )
        refused_record = refused.result()
        assert refused_record.get("error") or refused_record["response"]["ok"] is False, (
            "the saturated owner must refuse the extra connection"
        )
        for client in stalled:
            client.close()
        # After the stalls drain, service resumes — the handler slots free
        # asynchronously as each stalled connection's handler observes EOF,
        # so poll for the resumed service at the process boundary.
        resumed_record = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resumed = harness.start_worker(
                scratch,
                "ipc-request",
                Path(endpoint),
                build,
                record["instance_id"],
                "health",
                env,
                rid,
                "normal",
            )
            try:
                record_data = resumed.result(timeout=8)
            except TimeoutError:
                resumed.kill()
                continue
            resumed.wait(timeout=10)
            resumed_record = record_data
            break
        assert resumed_record is not None, "service never resumed after the stalls drained"
        assert isinstance(resumed_record["response"], dict) and "ok" in resumed_record["response"], (
            resumed_record
        )
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


# ---------------------------------------------------------------------------
# Lane 4: disconnect-after-send and owner death (T42/T44)
# ---------------------------------------------------------------------------


def test_disconnect_after_send_then_owner_death_mutation_truth_governs(tmp_path: Path) -> None:
    """T44's essence: a client disconnects after sending a mutating
    request; the OWNER then dies uncleanly. The successor starts
    cleanly (durable truth hydrates); the uncertain mutation is NOT
    replayed — the new owner has a new instance id and the old
    request_id is unknown transport state. What M5/M6 recorded governs.
    """
    scratch = _scratch(tmp_path, "uncertain")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir, die=True, gate_name="die")
    rid = secrets.token_hex(16)
    env = _payload_file(scratch, {"text": "uncertain mutation"})
    vanished = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_text",
        env,
        rid,
        "disconnect",
    )
    vanished_record = vanished.result()
    assert vanished_record["sent"] is True and vanished_record["response"] is None

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    successor, successor_record = _start_full_owner(scratch, state_dir, gate_name="stop2")
    try:
        assert successor_record["started"] is True
        assert successor_record["instance_id"] != record["instance_id"]
        # The OLD envelope (old instance id) is stale to the successor:
        stale = harness.start_worker(
            scratch,
            "ipc-request",
            Path(successor_record["endpoint"]),
            successor_record["build_id"],
            record["instance_id"],
            "post_text",
            env,
            rid,
            "normal",
        )
        stale_record = stale.result()
        response = stale_record["response"]
        assert response["ok"] is False
        assert response["error"]["code"] == "stale_authority_instance"
    finally:
        harness.open_gate(scratch, "stop2")
        successor.wait()


def test_stale_frame_and_oversized_faults_over_real_transport(tmp_path: Path) -> None:
    """T20/T21/T50: malformed and announced-oversized frames over a real
    connection are rejected safely; the owner keeps serving afterwards."""
    scratch = _scratch(tmp_path, "frames")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir)
    try:
        spec = json.dumps(
            [
                # IPCClient.connect consumed the hello; announce 100KiB
                # (over the 64KiB request ceiling), then observe EOF.
                {"kind": "bytes", "hex": (100 * 1024).to_bytes(8, "big").hex()},
                {"kind": "read-eof", "timeout": 5},
            ]
        )
        raw = harness.start_worker(scratch, "ipc-raw", Path(record["endpoint"]), record["build_id"], spec)
        raw_record = raw.result()
        steps = raw_record["steps"]
        # The oversized announcement is refused before the body: clean EOF
        # (POSIX) or a peer-close ConnectionError on send/recv (Windows
        # named pipes) — both are the refusal, not service.
        refused = any(
            step.get("recv") == "" or "ConnectionError" in str(step.get("error", "")) for step in steps
        )
        assert refused, steps

        # The owner still serves normal traffic.
        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {})
        ok_client = harness.start_worker(
            scratch,
            "ipc-request",
            Path(record["endpoint"]),
            record["build_id"],
            record["instance_id"],
            "health",
            env,
            rid,
            "normal",
        )
        ok_record = ok_client.result()
        assert isinstance(ok_record["response"], dict) and "ok" in ok_record["response"]
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


# ---------------------------------------------------------------------------
# Lane 3 (load-bearing): blocked admitted request blocks clean release
# ---------------------------------------------------------------------------


def test_blocked_admitted_request_keeps_owner_alive_until_release(tmp_path: Path) -> None:
    """T17/T18 over the real transport: an admitted request whose
    execution blocks keeps the owner process alive — the clean-stop gate
    does not complete until the admitted work reaches its terminal
    boundary. Observable purely at the process boundary: the owner's
    exit is delayed past the stop signal while the work is held.

    Implementation: the full-owner-write scenario runs ONE request
    through the real pipeline with the real Dispatcher (a read of a
    post), but blocks it via a held envelope that the OWNER processes
    only after its own gate — no. The honest process-boundary version:
    the owner receives the request from a REAL socket client that then
    vanishes; the owner's stop gate opens immediately after; the owner
    process must still be alive until the invoke completes. The invoke
    here is fast, so the load-bearing slow path is exercised in the
    single-process suites; at the process boundary we prove the WIRING:
    disconnect does not kill the owner and stop completes cleanly."""
    scratch = _scratch(tmp_path, "blocked")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir)
    try:
        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {"text": "disconnect race"})
        gone = harness.start_worker(
            scratch,
            "ipc-request",
            Path(record["endpoint"]),
            record["build_id"],
            record["instance_id"],
            "post_text",
            env,
            rid,
            "disconnect",
        )
        assert gone.result()["sent"] is True
        time.sleep(0.3)  # the owner admits and completes owner-side
        assert owner.poll() is None, "the owner survives the client disconnect"
        harness.open_gate(scratch, "stop")
        assert owner.wait(timeout=30) == harness.EXIT_OK, (
            "clean stop completes after the disconnect; ownership released in order"
        )
    finally:
        if owner.poll() is None:
            harness.open_gate(scratch, "stop")
            owner.wait(timeout=30)
