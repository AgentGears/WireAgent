"""M7 Layer 7 — real-process worker scenarios.

Every scenario runs REAL production objects: AuthorityOwnerLock,
AuthoritySession, and — for the full-owner scenarios — the real
Dispatcher with production IPC enabled. The only stubs are the browser
DOM port and the session-manager attach (the browser itself is out of
scope for Layer 7 and its absence is what Layer 8 qualifies separately).

Contract: argv[1] is the JSON result path; remaining args are scenario
parameters. The result file is written as soon as the scenario's first
stable observation exists (BEFORE any gate wait), so the controller can
proceed while the worker holds state. Gate files are TEST ORCHESTRATION
only — production code never reads them.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# Match tests/conftest.py for CI's --no-deps editable install.
try:
    import super_browser  # noqa: F401
except ImportError:  # pragma: no cover - CI-only subprocess path
    sys.path.insert(0, str(Path(__file__).parent / "stubs"))

from webwire.authority import AuthorityBusyError, AuthorityOwnerLock  # noqa: E402


def _write(result_path: Path, payload: dict[str, Any]) -> None:
    result_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _wait_gate(path: Path, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"gate never opened: {path}")
        time.sleep(0.02)


# ---------------------------------------------------------------------------
# Lock-level scenarios (no Dispatcher)
# ---------------------------------------------------------------------------


def lock_hold(result_path: Path, state_dir: Path, release_gate: Path) -> int:
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23
    _write(result_path, {"acquired": True, "busy": False})
    _wait_gate(release_gate)
    lock.release()
    return 0


def lock_probe(result_path: Path, state_dir: Path) -> int:
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23
    try:
        _write(result_path, {"acquired": True, "busy": False})
        return 0
    finally:
        lock.release()


def owner_die(result_path: Path, state_dir: Path, die_gate: Path) -> int:
    """Acquire, report, then DIE UNCLEANLY on the gate (os._exit: no
    finally, no release, no atexit — process death is the only cleanup)."""
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23
    _write(result_path, {"acquired": True, "busy": False})
    _wait_gate(die_gate)
    os._exit(9)


def owner_hung(result_path: Path, state_dir: Path, scratch: Path) -> int:
    """Acquire, report, then HANG (never releases; the controller must
    terminate the process — the OS is the only recovery)."""
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23
    _write(result_path, {"acquired": True, "busy": False})
    time.sleep(600)
    return 0


# ---------------------------------------------------------------------------
# Full-owner scenarios (real Dispatcher + production IPC)
# ---------------------------------------------------------------------------


class _StubSB:
    _page = None
    _controller = None
    allowed_methods: set = set()


def _build_dispatcher(state_dir: Path, block_submit_path: Path):
    from webwire.config import WebWireConfig
    from webwire.dispatcher import Dispatcher
    from webwire.session import SessionManager

    class _RecordingSessionManager(SessionManager):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True
            self._resolved_handle = "@owner"

        async def start(self) -> Any:
            from webwire.envelope import ok_result

            return ok_result(data={})

        async def stop(self) -> Any:
            from webwire.envelope import ok_result

            return ok_result(data={})

    cfg = WebWireConfig(state_dir=state_dir, kill_env_var=None)
    sm = _RecordingSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=sm, enable_ipc=True)  # type: ignore[arg-type]
    from types import SimpleNamespace

    dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
        lambda sb: setattr(dispatcher, "_m5_stack", SimpleNamespace(read_broker=_StubSB()))
    )
    return dispatcher


def full_owner(
    result_path: Path,
    state_dir: Path,
    release_gate: Path,
    die: bool,
) -> int:
    """Start a REAL Dispatcher with production IPC (named pipe / domain
    socket), report instance id + endpoint + media staging root, then
    either stop cleanly on the gate or die uncleanly on it."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(
                result_path,
                {"started": False, "error": getattr(started.error, "message", str(started))},
            )
            return 5
        transport = dispatcher._ipc_transport
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        # The gate wait runs OFF the loop: the transport dispatches
        # admitted work onto THIS loop, so blocking it here would
        # deadlock every in-flight request.
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, release_gate, 120.0)
        if die:
            os._exit(9)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def full_owner_write(
    result_path: Path,
    state_dir: Path,
    release_gate: Path,
    envelope_path: Path,
    response_path: Path,
    die_after_admission: bool,
) -> int:
    """A full owner that also performs one INVOCATION read from a file
    (the envelope as JSON), writing the framed-decoded response — used
    for in-owner admission/lifecycle scenarios without a second socket
    client (the real transport tests cover the socket side)."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        envelope = json.loads(envelope_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
        from webwire.authority_ipc_framing import encode_json_frame, parse_frame_header

        frame = encode_json_frame(envelope)
        header, consumed = parse_frame_header(frame)
        response = await dispatcher._ipc_server.process_request(header, frame[consumed:])
        decoded = json.loads(response[8:])
        _write(response_path, decoded)
        if die_after_admission:
            os._exit(9)
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, release_gate, 120.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# IPC client scenarios (real transport, real faults)
# ---------------------------------------------------------------------------


def ipc_request(
    result_path: Path,
    endpoint: Path,
    build_id: str,
    instance_id: str,
    operation: str,
    payload_path: Path,
    request_id: str,
    disconnect_after_send: bool,
) -> int:
    """Connect to the real endpoint, verify hello, send ONE framed
    request. Either read the response (normal) or close the socket
    immediately after sending (disconnect-after-send fault)."""
    import asyncio

    async def _run() -> int:
        from webwire.authority_ipc_framing import encode_json_frame
        from webwire.authority_ipc_transport import IPCClient

        payload = json.loads(payload_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
        envelope = {
            "protocol_version": 1,
            "operation": operation,
            "payload": payload,
            "request_id": request_id,
            "authority_instance_id": instance_id,
            "runtime_build_id": build_id,
        }

        def _do() -> dict[str, Any]:
            client = IPCClient(str(endpoint), expected_build_id=build_id)
            try:
                client.connect()
                if disconnect_after_send:
                    client._sock.send(encode_json_frame(envelope))
                    client._sock.close()
                    return {"sent": True, "response": None}
                return {"sent": True, "response": client.request(envelope)}
            except Exception as exc:  # noqa: BLE001 - record the fault
                return {"sent": True, "error": repr(exc)}

        decoded = await asyncio.to_thread(_do)
        _write(result_path, decoded)
        return 0

    return asyncio.run(_run())


def ipc_raw(
    result_path: Path,
    endpoint: Path,
    build_id: str,
    frames_spec: str,
) -> int:
    """Send RAW bytes (announced-oversized / malformed / duplicate-id
    frames) over a real connection and record what the owner did."""
    import asyncio

    async def _run() -> int:
        from webwire.authority_ipc_transport import IPCClient

        spec = json.loads(frames_spec)
        client = IPCClient(str(endpoint), expected_build_id=build_id)
        client.connect()
        sock = client._sock
        outcomes: list[dict[str, Any]] = []
        for step in spec:
            kind = step.get("kind")
            try:
                if kind == "bytes":
                    import binascii

                    sock.send(binascii.unhexlify(step["hex"]))
                    outcomes.append({"sent": True})
                elif kind == "request":
                    from webwire.authority_ipc_framing import encode_json_frame

                    sock.send(encode_json_frame(step["envelope"]))
                    outcomes.append({"sent": True})
                elif kind == "read-eof":
                    sock.settimeout(step.get("timeout", 5))
                    try:
                        data = sock.recv(1)
                        outcomes.append({"recv": data.hex() if data else ""})
                    except ConnectionError:
                        # A closed pipe peer surfaces as ConnectionError on
                        # Windows and as clean EOF on POSIX — both are EOF.
                        outcomes.append({"recv": ""})
                elif kind == "read-response":
                    from webwire.authority_ipc_transport import _bounded_receive

                    _, resp = _bounded_receive(sock, ceiling=1 << 20)
                    outcomes.append({"response": resp})
            except Exception as exc:  # noqa: BLE001
                outcomes.append({"error": repr(exc)})
        client.close()
        _write(result_path, {"steps": outcomes})
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# POSIX-only child-survival scenario
# ---------------------------------------------------------------------------


def fork_child(result_path: Path, state_dir: Path, release_gate: Path) -> int:
    """(POSIX only) Acquire ownership, FORK a child, parent dies
    uncleanly; the child — which inherited the raw descriptor — must be
    unable to keep the domain locked: a successor must acquire while the
    child lives. The child writes its own record and exits on the gate."""
    import os as _os

    if not hasattr(_os, "fork"):
        _write(result_path, {"unsupported": True})
        return 27

    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23

    child_result = result_path.with_suffix(".child.json")
    pid = _os.fork()
    if pid == 0:
        # Child: inherited descriptors only, no release path. Hold until
        # the gate, then exit WITHOUT releasing (it never owned).
        _write(child_result, {"child_alive": True, "pid": _os.getpid()})
        try:
            _wait_gate(release_gate, timeout=60)
        except Exception:
            pass
        _os._exit(0)

    _write(result_path, {"acquired": True, "busy": False, "child_pid": pid})
    _os._exit(9)  # parent dies uncleanly with the child alive


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    scenario = argv[1]
    result_path = Path(argv[2])
    raw = argv[3:]

    if scenario == "lock-hold":
        return lock_hold(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "lock-probe":
        return lock_probe(result_path, Path(raw[0]))
    if scenario == "owner-die":
        return owner_die(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "owner-hung":
        return owner_hung(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "full-owner":
        return full_owner(result_path, Path(raw[0]), Path(raw[1]), raw[2] == "die")
    if scenario == "full-owner-write":
        return full_owner_write(
            result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]), Path(raw[3]), raw[4] == "die"
        )
    if scenario == "ipc-request":
        return ipc_request(
            result_path,
            Path(raw[0]),
            raw[1],
            raw[2],
            raw[3],
            Path(raw[4]),
            raw[5],
            raw[6] == "disconnect",
        )
    if scenario == "ipc-raw":
        return ipc_raw(result_path, Path(raw[0]), raw[1], raw[2])
    if scenario == "fork-child":
        return fork_child(result_path, Path(raw[0]), Path(raw[1]))
    print(f"unknown scenario {scenario}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
