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
import secrets
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

QUAL_TABLE_BOUND = 8


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


def lock_race(
    result_path: Path,
    state_dir: Path,
    acquire_gate: Path,
    release_gate: Path,
) -> int:
    """TRUE simultaneous acquisition (F-92): signal ready, wait for the
    COMMON acquire gate (both contenders barrier here), then race
    acquire() against the sibling. Exactly one wins."""
    ready = result_path.with_suffix(".ready")
    _write(ready, {"ready": True})
    _wait_gate(acquire_gate)
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

    def __getattr__(self, name: str) -> Any:
        """Any OTHER broker method the real capabilities probe becomes an
        async ok-result: the DOM surface is out of Layer-7 scope, and the
        laws under qualification (retention, canonical identity, refusal)
        must not depend on which diagnostics a capability happens to
        touch. Underscore names raise normally (real attributes only)."""
        if name.startswith("_"):
            raise AttributeError(name)

        async def _generic(*args: Any, **kwargs: Any) -> Any:
            from webwire.envelope import ok_result

            return ok_result(data={})

        return _generic


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

    # The stub stack carries placeholder executors for EVERY migrated
    # write family: write-kernel-level barriers fire BEFORE any executor
    # method runs, so the placeholders are never called in the barrier
    # scenarios — but the Dispatcher's adapter lookup requires the
    # attributes to EXIST.
    dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
        lambda sb: setattr(
            dispatcher,
            "_m5_stack",
            SimpleNamespace(
                read_broker=_StubSB(),
                post_text_executor=object(),
                reply_executor=object(),
                quote_executor=object(),
                delete_executor=object(),
                effect_executor=object(),
                media_executor=object(),
            ),
        )
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
    send_gate: Path = None,  # type: ignore[assignment]
) -> int:
    """Connect to the real endpoint, verify hello, send ONE framed
    request. Either read the response (normal) or close the socket
    immediately after sending (disconnect-after-send fault)."""
    import asyncio

    async def _run() -> int:
        from webwire.authority_ipc_framing import encode_json_frame
        from webwire.authority_ipc_transport import IPCClient

        payload = json.loads(payload_path.read_text(encoding="utf-8"))  # noqa: ASYNC240  # noqa: ASYNC240
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
                _write(result_path.with_suffix(".connected"), {"connected": True})
                if send_gate is not None:
                    # T16's in-window racer: CONNECT before the drain
                    # seam, DELIVER after it — the already-connected
                    # socket receives the stable draining refusal.
                    _wait_gate(send_gate, timeout=120)
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
                    # No per-socket timeout (F-98: production exposes none on
                    # pipes); the CONTROLLER bounds this probe — the expected
                    # peer close arrives promptly, or the worker is killed and
                    # the test fails.
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
    child lives. The child writes its own record — including its
    self-reported kernel start time (F-123: the controller proves the
    SAME fork child survives the parent's death through takeover, not
    merely that a child once existed) — and exits on the gate."""
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
        record: dict[str, Any] = {"child_alive": True, "pid": _os.getpid()}
        self_stat = Path("/proc/self/stat")
        if self_stat.exists():
            try:
                raw = self_stat.read_text(encoding="utf-8")
                fields = raw[raw.rindex(")") + 1 :].split()
                record["starttime"] = int(fields[19])
            except (OSError, ValueError, IndexError):
                pass
        _write(child_result, record)
        try:
            _wait_gate(release_gate, timeout=60)
        except Exception:
            pass
        _os._exit(0)

    _write(result_path, {"acquired": True, "busy": False, "child_pid": pid})
    _os._exit(9)  # parent dies uncleanly with the child alive


# ---------------------------------------------------------------------------
# Controlled execution barriers INSIDE admitted owner work (F-93/F-94/F-96)
# ---------------------------------------------------------------------------


def _wrap_write_kernel_with_barrier(
    dispatcher, events_path: Path, exec_gate: Path, match: str, block_all: bool = False
) -> None:
    """Wrap the WRITE KERNEL's execute with a controlled execution
    barrier INSIDE admitted owner work (F-93/F-94/F-96C). The barrier
    sits inside ``_invoke_inner`` under the Dispatcher's invocation
    lock — the point where the request is admitted, retained-table
    pinned, and (for media) artifact-pinned. Blocking here while
    ``Dispatcher.stop()`` begins is exactly the drain law under
    qualification; a barrier placed BEFORE the invocation lock would
    deadlock stop() against the released call re-entering the lock.
    Test orchestration in THIS process only."""
    import asyncio

    kernel = dispatcher._write_kernel
    original = kernel.execute

    async def _barrier_execute(write_cap, broker, input, **kwargs):
        # Default: only the CONFIRM (token-bearing) call blocks — the
        # preview phase must complete so qualification clients can obtain
        # real tokens. block_all (the tablesat scenario) blocks every
        # call: each holds its retained-table entry while blocked INSIDE
        # the invocation lock, which is exactly the drain-order shape
        # stop() expects (an invoke-seam barrier OUTSIDE the lock
        # deadlocks stop()'s lock-then-drain ordering).
        carries_token = isinstance(input, dict) and input.get("confirmation_token")
        should_block = block_all or carries_token
        if should_block and (match == "*" or getattr(write_cap, "name", "") == match):
            with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                fh.write(json.dumps({"admitted": getattr(write_cap, "name", "?")}) + "\n")
            await asyncio.get_event_loop().run_in_executor(None, _wait_gate, exec_gate, 120.0)
        return await original(write_cap, broker, input, **kwargs)

    kernel.execute = _barrier_execute


def full_owner_blocked(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    exec_gate: Path,
    events_path: Path,
    invoke_match: str,
) -> int:
    """A full production owner whose ADMITTED work blocks at a
    controlled post-admission barrier (F-93/F-94). The stop sequence
    begins while work is blocked: the owner must stay alive, keep the
    domain locked, and only complete the clean stop after the barrier
    releases and the admitted work terminalizes."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        _wrap_write_kernel_with_barrier(dispatcher, events_path, exec_gate, invoke_match)
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 120.0)
        _write(result_path.with_suffix(".stopping"), {"stop_begun": True})
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def full_owner_media_block(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    exec_gate: Path,
    events_path: Path,
) -> int:
    """F-96C: a full owner with the media registry live; ADMITTED media
    mutations block at the post-admission barrier WITH their artifacts
    pinned (the IPC server pins around the wrapped invoke). Stop begins
    while pinned: the artifact must survive until the work terminalizes,
    then post-drain retention reclaims it."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        _wrap_write_kernel_with_barrier(dispatcher, events_path, exec_gate, "post_photo")
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 120.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def ingest_crash(
    result_path: Path,
    state_dir: Path,
    die_gate: Path,
) -> int:
    """F-96B: a full owner whose NEXT ingest acquires its bounded
    temporary copy and then DIES at the pre-publish barrier — leaving
    the .tmp residue and NO content-addressed object."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        registry = dispatcher._media_registry
        original_acquire = registry._acquire_bounded_copy

        def _acquire_then_die(source_fd: int) -> Path:
            original_acquire(source_fd)  # the temp copy exists from here
            _write(result_path.with_suffix(".temp"), {"temp_acquired": True})
            _wait_gate(die_gate, timeout=60)
            os._exit(9)

        registry._acquire_bounded_copy = _acquire_then_die  # type: ignore[method-assign]
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, die_gate, 120.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Real-M5 controlled crash point (F-95): RESERVED durable, then death
# ---------------------------------------------------------------------------


class _BlockingPostPort:
    """The real post-text DOM port with a controlled barrier INSIDE
    click_submit — the point where the M5 attempt is already RESERVED
    durably. Test orchestration in THIS process only."""

    def __init__(self, die_gate: Path, blocked_marker: Path) -> None:
        self.die_gate = die_gate
        self.blocked_marker = blocked_marker
        self.composer_text = ""

    async def fill_composer(self, text: str) -> Any:
        self.composer_text = text
        from webwire.envelope import ok_result

        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"count": 0})

    async def close_composer(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"cleanup": "closed"})

    async def attach_media(self, image_path: str) -> Any:
        raise AssertionError("plain post must not attach media")

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"opened": True})

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"opened": True})

    async def click_submit(
        self, *, _commit_gate, _precommit_check, _expected_text, _expected_attachments
    ) -> Any:
        # The durable RESERVED row is written INSIDE the commit gate; the
        # controlled crash point sits AFTER it returns clean — the attempt
        # is reserved durably, the effect may or may not exist.
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        _write(self.blocked_marker, {"m5_submit_blocked": True, "expected": _expected_text})
        _wait_gate(self.die_gate, timeout=60)
        os._exit(9)  # die INSIDE the admitted mutation, RESERVED durable


class _DieEvidence:
    async def capture_pre_submit_ids(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(self, pre_submit_ids: set, *, exclude_ids=None) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"post_id": "99", "post_url": "https://x.com/owner/status/99"})

    async def verify_post_text(self, post_url: str, normalized_text: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(
            data={
                "text_matches": True,
                "direct_status_owned": True,
                "post_actor": "owner",
                "post_id": "99",
                "post_url": "https://x.com/owner/status/99",
            }
        )


def full_owner_m5_serving(
    result_path: Path,
    state_dir: Path,
    die_gate: Path,
) -> int:
    """F-101/T44 SERVING owner: the REAL M5 post-text executor stack with
    the production IPC endpoint LIVE. The controller drives preview and
    confirm from EXTERNAL client processes over the real transport; the
    executor's click_submit dies AFTER the commit gate (durable RESERVED)
    at the die gate. The confirm's response can never exist — the client
    is gone and the owner dies inside the mutation."""
    import asyncio

    async def _run() -> int:
        from types import SimpleNamespace

        from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
        from webwire.safety.m5_actor_bound_post_executor import M5ActorBoundPostTextExecutor
        from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
        from webwire.safety.scoped_authority import ScopedAuthorityBroker

        dispatcher = _build_dispatcher(state_dir, state_dir)
        blocked_marker = result_path.with_suffix(".blocked")
        port = _BlockingPostPort(die_gate, blocked_marker)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        scoped = ScopedAuthorityBroker(port, dispatcher._m5_gateway, policies=DEFAULT_EFFECT_POLICIES)
        runtime = M5ExecutionRuntime(
            scoped_authority=scoped,
            commit_gateway=dispatcher._m5_gateway,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        dispatcher._m5_stack = SimpleNamespace(
            read_broker=_StubSB(),
            post_text_executor=M5ActorBoundPostTextExecutor(runtime=runtime, evidence_reader=_DieEvidence()),
        )
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        # Serve until the executor dies inside the mutation (the die gate
        # is opened by the controller after verifying the durable state).
        # Off-loop so in-flight transport work keeps progressing.
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, die_gate, 240.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def full_owner_tablesat(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    exec_gate: Path,
    events_path: Path,
) -> int:
    """F-94's T57 scenario: a full owner whose transport capacity is
    raised ABOVE the retained-table bound for THIS qualification (the
    production capacity=4 would turn table pressure into connection
    backpressure — a different, separately-tested law). Every request
    logs an ADMISSION event at the invoke seam (post table
    registration) and blocks INSIDE the write kernel under the
    invocation lock; the first enters the kernel, the rest queue on the
    Dispatcher's single invocation lock — all admitted, all pinned."""
    import asyncio

    import webwire.authority_ipc_server as ipc_module

    async def _run() -> int:
        # Qualification-only constant overrides (test orchestration in
        # THIS process; the Dispatcher reads both at start() time):
        # transport capacity above the fill count, and the retained
        # table bound lowered to 8. The LAW under qualification —
        # bounded inflight, table_full backpressure, join-not-reexecute,
        # release-then-terminalize — is independent of the constant's
        # production value; the production value 64 would make each
        # released invoke re-hydrate the full recovery projection and
        # the drain would take minutes without adding evidence.
        original_cap = ipc_module.IPC_SERVER_MAX_CONCURRENT
        ipc_module.IPC_SERVER_MAX_CONCURRENT = 128
        # RetainedRequestTable's bound is a DEF-TIME default parameter —
        # patching the constant does nothing. Patch the NAME the server
        # module resolves at construction time instead, forcing the
        # smaller qualification bound.
        original_table_cls = ipc_module.RetainedRequestTable

        class _SmallRetainedTable(original_table_cls):  # type: ignore[misc, valid-type]
            def __init__(self, **kwargs: Any) -> None:
                kwargs["max_inflight"] = QUAL_TABLE_BOUND
                super().__init__(**kwargs)

        ipc_module.RetainedRequestTable = _SmallRetainedTable
        try:
            dispatcher = _build_dispatcher(state_dir, state_dir)
            started = await dispatcher.start()
        finally:
            ipc_module.IPC_SERVER_MAX_CONCURRENT = original_cap
            ipc_module.RetainedRequestTable = original_table_cls
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        # The barrier sits INSIDE the write kernel (under the invocation
        # lock), blocking EVERY call: each admitted request holds its
        # retained-table entry while blocked — table pressure — and the
        # lock ordering matches stop()'s drain sequence. (An invoke-seam
        # barrier OUTSIDE the lock deadlocks stop()'s lock-then-drain
        # ordering; blocking only confirms would let previews complete
        # and the table would never fill.)
        _wrap_write_kernel_with_barrier(dispatcher, events_path, exec_gate, "*", block_all=True)
        # EVIDENCE seam: log every request that reaches invoke (all
        # admitted requests — the kernel serializes, so only the first
        # enters the barrier; the rest queue on the invocation lock
        # while holding their retained-table entries). Pass-through: no
        # barrier here (an invoke-seam barrier outside the lock
        # deadlocks stop()'s lock-then-drain ordering).
        original_invoke = dispatcher._invoke_admitted

        async def _logging_invoke(name: str, payload: dict) -> Any:
            with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                fh.write(json.dumps({"invoke": name}) + "\n")
            return await original_invoke(name, payload)

        dispatcher._ipc_server._invoke = _logging_invoke
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 240.0)
        _write(result_path.with_suffix(".stopping"), {"stop_begun": True})
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def ipc_flood(
    result_path: Path,
    endpoint: Path,
    build_id: str,
    instance_id: str,
    operation: str,
    payload_path: Path,
    count: int,
    duplicate_of_first: bool,
    explicit_request_id: str = "",
) -> int:
    """Send COUNT unique-request-id requests, ONE PER CONNECTION (the
    wire law), disconnecting each immediately after send — from THIS one
    process (spawning one client process per request is needlessly
    slow). ``explicit_request_id`` (F-100): when supplied, the FIRST
    send uses THIS id instead of a fresh one — the controller duplicates
    an ACTUALLY BLOCKED table entry, not a fresh id the full table
    would refuse. ``duplicate_of_first`` then re-sends that same id once
    more (the duplicate-delivery probe)."""
    import asyncio

    async def _run() -> int:
        from webwire.authority_ipc_framing import encode_json_frame
        from webwire.authority_ipc_transport import IPCClient

        payload = json.loads(payload_path.read_text(encoding="utf-8"))  # noqa: ASYNC240
        first_rid = explicit_request_id or secrets.token_hex(16)

        def _do() -> dict[str, Any]:
            sent = 0
            errors: list[str] = []
            for index in range(count):
                rid = first_rid if index == 0 else secrets.token_hex(16)
                client = IPCClient(str(endpoint), expected_build_id=build_id)
                try:
                    client.connect()
                    client._sock.send(
                        encode_json_frame(
                            {
                                "protocol_version": 1,
                                "operation": operation,
                                "payload": payload,
                                "request_id": rid,
                                "authority_instance_id": instance_id,
                                "runtime_build_id": build_id,
                            }
                        )
                    )
                    client._sock.close()
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
            if duplicate_of_first:
                client = IPCClient(str(endpoint), expected_build_id=build_id)
                try:
                    client.connect()
                    client._sock.send(
                        encode_json_frame(
                            {
                                "protocol_version": 1,
                                "operation": operation,
                                "payload": payload,
                                "request_id": first_rid,
                                "authority_instance_id": instance_id,
                                "runtime_build_id": build_id,
                            }
                        )
                    )
                    client._sock.close()
                    sent += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append(repr(exc))
            return {"sent": sent, "errors": errors, "first_request_id": first_rid}

        decoded = await asyncio.to_thread(_do)
        _write(result_path, decoded)
        return 0

    return asyncio.run(_run())


def full_owner_counting(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    events_path: Path,
) -> int:
    """F-106's generic qualification owner: a plain full production
    owner whose Dispatcher invocation seam writes ONE process-visible
    event per actual invoke. The T21/T23/T58 laws assert execution
    counts against this file — 'rejected before execution' and 'no new
    execution' become explicit rather than inferred from response
    bytes."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        original_invoke = dispatcher._invoke_admitted

        async def _counting_invoke(name: str, payload: dict) -> Any:
            with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                fh.write(json.dumps({"invoke": name}) + "\n")
            return await original_invoke(name, payload)

        dispatcher._ipc_server._invoke = _counting_invoke
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 120.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Round two: the durable-state crash matrix (T34/T36/T37), the
# takeover-timing observer (T48), and the reconciliation-append crash
# (T38).
# ---------------------------------------------------------------------------


class _CrashPointPort_cls:
    """The real post-text DOM port with a PARAMETERIZED death point:

    - ``pre-reserved``: die during composer fill — the confirm was
      admitted but nothing was submitted and NO durable row exists.
    - ``reserved``: die after the commit gate (durable RESERVED) but
      before the click returns (the T44 point, kept for the matrix).
    - ``post-effect``: the gate consumed and the click RETURNED success
      (the external effect plausibly exists) — death before any
      evidence capture.
    - ``post-terminal``: the port never dies; a kernel-level wrapper
      dies AFTER the full execution returns (terminal durable state).
    """

    def __init__(self, point: str, die_gate: Path, marker: Path) -> None:
        self.point = point
        self.die_gate = die_gate
        self.marker = marker
        self.composer_text = ""

    def _die(self, detail: str) -> None:
        _write(self.marker, {"crash_point": self.point, "at": detail})
        _wait_gate(self.die_gate, timeout=60)
        os._exit(9)

    async def fill_composer(self, text: str) -> Any:
        if self.point == "pre-reserved":
            self._die("fill_composer")
        self.composer_text = text
        from webwire.envelope import ok_result

        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"count": 0})

    async def close_composer(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"cleanup": "closed"})

    async def attach_media(self, image_path: str) -> Any:
        raise AssertionError("plain post must not attach media")

    async def click_submit(
        self, *, _commit_gate, _precommit_check, _expected_text, _expected_attachments
    ) -> Any:
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.point == "reserved":
            self._die("after_commit_gate")
        # The click itself completes: the external effect plausibly exists.
        from webwire.envelope import ok_result

        return ok_result(data={"submitted": True})

    async def capture_pre_submit_ids(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(self, pre_submit_ids: set, *, exclude_ids=None) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"post_id": "99", "post_url": "https://x.com/owner/status/99"})

    async def verify_post_text(self, post_url: str, normalized_text: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(
            data={
                "text_matches": True,
                "direct_status_owned": True,
                "post_actor": "owner",
                "post_id": "99",
                "post_url": "https://x.com/owner/status/99",
            }
        )


class _CrashPointEvidence:
    """The post-text evidence reader with the POST-EFFECT death point:
    the executor captures evidence through THIS object (not the write
    port), so the after-effect/before-evidence crash lives here. All
    other points keep the port-side deaths."""

    def __init__(self, point: str, die_gate: Path, marker: Path) -> None:
        self.point = point
        self.die_gate = die_gate
        self.marker = marker

    def _die(self, detail: str) -> None:
        _write(self.marker, {"crash_point": self.point, "at": detail})
        _wait_gate(self.die_gate, timeout=60)
        os._exit(9)

    async def capture_pre_submit_ids(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(self, pre_submit_ids: set, *, exclude_ids=None) -> Any:
        if self.point == "post-effect":
            self._die("before_evidence_capture")
        from webwire.envelope import ok_result

        return ok_result(data={"post_id": "99", "post_url": "https://x.com/owner/status/99"})

    async def verify_post_text(self, post_url: str, normalized_text: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(
            data={
                "text_matches": True,
                "direct_status_owned": True,
                "post_actor": "owner",
                "post_id": "99",
                "post_url": "https://x.com/owner/status/99",
            }
        )


def full_owner_m5_crash_at(
    result_path: Path,
    state_dir: Path,
    die_gate: Path,
    point: str,
) -> int:
    """The T44 serving shape with the crash point PARAMETERIZED for the
    T34/T36/T37 matrix: external clients preview/confirm over the real
    transport; the REAL actor-bound post-text executor runs; death
    lands at the requested durable-state boundary."""
    import asyncio

    async def _run() -> int:
        from types import SimpleNamespace

        from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
        from webwire.safety.m5_actor_bound_post_executor import M5ActorBoundPostTextExecutor
        from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
        from webwire.safety.scoped_authority import ScopedAuthorityBroker

        dispatcher = _build_dispatcher(state_dir, state_dir)
        marker = result_path.with_suffix(".crash")
        port = _CrashPointPort_cls(point, die_gate, marker)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        scoped = ScopedAuthorityBroker(port, dispatcher._m5_gateway, policies=DEFAULT_EFFECT_POLICIES)
        runtime = M5ExecutionRuntime(
            scoped_authority=scoped,
            commit_gateway=dispatcher._m5_gateway,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        dispatcher._m5_stack = SimpleNamespace(
            read_broker=_StubSB(),
            post_text_executor=M5ActorBoundPostTextExecutor(
                runtime=runtime,
                evidence_reader=_CrashPointEvidence(point, die_gate, marker),
            ),
        )
        if point == "post-terminal":
            # Die AFTER the token-bearing kernel execution fully returns —
            # the terminal durable state is written; only the response
            # (to an already-disconnected client) never existed.
            kernel = dispatcher._write_kernel
            original_execute = kernel.execute

            async def _die_after_execute(write_cap, broker, input, **kwargs):
                result = await original_execute(write_cap, broker, input, **kwargs)
                if isinstance(input, dict) and input.get("confirmation_token"):
                    _write(marker, {"crash_point": point, "at": "after_terminal_result"})
                    await asyncio.get_event_loop().run_in_executor(None, _wait_gate, die_gate, 60)
                    os._exit(9)
                return result

            kernel.execute = _die_after_execute
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
                "crash_point": point,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, die_gate, 240.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def full_owner_observing(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    phases_path: Path,
) -> int:
    """T48's observer: a full production owner that records the ORDER of
    its start phases to a process-visible file — each successful
    acquisition, the recovery HYDRATION, and the BROWSER stack
    installation — so a successor's takeover timing is provable at the
    process boundary (hydrate BEFORE browser)."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)

        def _phase(name: str) -> None:
            with open(phases_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"phase": name}) + "\n")

        original_hydrate = dispatcher._m5_recovery.hydrate

        def _observed_hydrate() -> Any:
            _phase("hydrating")
            try:
                original_hydrate()
            except BaseException:
                _phase("hydrate-failed")
                raise
            _phase("hydrated")
            return None

        dispatcher._m5_recovery.hydrate = _observed_hydrate

        # F-110: the browser/session boundary is SessionManager.start()
        # ENTRY - production _start_locked() runs hydrate() BEFORE
        # session.start(); _install_m5_live_stack sits AFTER it and
        # cannot prove hydrate-before-browser. The law is
        # hydrated < browser-start-called < ready.
        original_start = dispatcher._session.start

        async def _observed_start() -> Any:
            _phase("browser-start-called")
            return await original_start()

        dispatcher._session.start = _observed_start  # type: ignore[method-assign]

        def _observed_install(sb: Any) -> None:
            _phase("m5-stack-installing")
            from types import SimpleNamespace

            dispatcher._m5_stack = SimpleNamespace(read_broker=_StubSB())

        dispatcher._install_m5_live_stack = _observed_install  # type: ignore[method-assign]

        _phase("acquiring")
        started = await dispatcher.start()
        if not started.ok:
            _phase("start-refused")
            _write(result_path, {"started": False})
            return 5
        _phase("ready")
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 240.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def recon_append_crash(
    result_path: Path,
    state_dir: Path,
    die_gate: Path,
) -> int:
    """T38: a full owner whose NEXT reconciliation-ledger append writes
    a TORN final line (half the JSON, no newline, flushed) and then
    dies uncleanly — the on-disk shape a mid-append crash leaves. The
    successor must exhibit whatever the EXISTING M6 semantics dictate
    for that state (fail-closed ambiguity handling or tolerated tail),
    unchanged by the process boundary."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        ledger = dispatcher._m6_reconciliation._reconciliation_ledger
        ledger_path = ledger.path
        # F-111: the production append builds its own JSONL payload
        # (record.to_jsonl()) and writes it via os.open + os.write. The
        # fault intercepts the FIRST real write to the ledger's fd: half
        # the PRODUCTION bytes go out through the REAL syscall, then the
        # process dies mid-append - no synthetic line is constructed.
        real_open, real_write = os.open, os.write
        target_fds: set = set()
        fault_state = {"armed": True}

        def _faulting_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            fd = real_open(path, flags, *args, **kwargs)
            if fault_state["armed"] and os.fspath(path) == str(ledger_path):
                target_fds.add(fd)
            return fd

        def _faulting_write(fd: int, view: Any) -> int:
            if fault_state["armed"] and fd in target_fds:
                fault_state["armed"] = False
                half = max(1, len(view) // 2)  # torn: no newline survives
                # Record the REAL syscall's return value (not the
                # requested count): the torn-append evidence is the
                # bytes the kernel actually accepted.
                written = real_write(fd, view[:half])
                _write(result_path.with_suffix(".torn"), {"torn": True, "requested": half, "written": written})
                _wait_gate(die_gate, timeout=60)
                os._exit(9)
            return real_write(fd, view)

        os.open = _faulting_open  # type: ignore[assignment]
        os.write = _faulting_write  # type: ignore[assignment]
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, die_gate, 240.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


def full_owner_t16(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    exec_gate: Path,
    events_path: Path,
) -> int:
    """F-112's T16 owner: confirmed work blocks in the kernel; the DRAIN
    SEAM (AuthoritySession.begin_drain ENTRY) writes a process-visible
    marker the controller synchronizes on; every actual Dispatcher
    invocation is logged so admission is explicit."""
    import asyncio

    async def _run() -> int:
        dispatcher = _build_dispatcher(state_dir, state_dir)
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5
        _wrap_write_kernel_with_barrier(dispatcher, events_path, exec_gate, "*")
        original_invoke = dispatcher._invoke_admitted

        async def _logging_invoke(name: str, payload: dict) -> Any:
            with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                fh.write(json.dumps({"invoke": name}) + "\n")
            return await original_invoke(name, payload)

        dispatcher._ipc_server._invoke = _logging_invoke
        session = dispatcher._authority_session
        original_begin_drain = session.begin_drain
        seam = result_path.with_suffix(".drain")

        def _observed_begin_drain() -> None:
            _write(seam, {"drain_beginning": True})
            original_begin_drain()

        session.begin_drain = _observed_begin_drain  # type: ignore[method-assign]
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 240.0)
        _write(result_path.with_suffix(".stopping"), {"stop_begun": True})
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# T63: POSIX spawn/exec child survival (extends the fork lane)
# ---------------------------------------------------------------------------


def owner_spawn_child(
    result_path: Path, state_dir: Path, die_gate: Path, child_gate: Path
) -> int:
    """(POSIX only) T63's spawn/exec parent: acquire the domain, spawn a
    REAL exec'd child — a fresh interpreter image through the qualified
    subprocess path, launched with close_fds=False so the ONLY thing
    standing between the child and the owner descriptor is the
    production close-on-exec contract — then die uncleanly on the gate
    with the child still alive."""
    import subprocess as _sp

    if os.name != "posix":
        _write(result_path, {"unsupported": True})
        return 27

    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _write(result_path, {"acquired": False, "busy": True})
        return 23

    child_result = result_path.with_suffix(".child.json")
    child = _sp.Popen(  # noqa: S603 - fixed interpreter + repo worker file
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "child-exec",
            str(child_result),
            str(state_dir),
            str(child_gate),
        ],
        cwd=str(Path(__file__).resolve().parent.parent),
        close_fds=False,  # adverse setting: close-on-exec alone must suffice
    )
    _write(result_path, {"acquired": True, "busy": False, "child_pid": child.pid})
    _wait_gate(die_gate)
    os._exit(9)  # parent dies uncleanly with the exec'd child alive


def child_exec(result_path: Path, state_dir: Path, release_gate: Path) -> int:
    """(POSIX only) T63's exec'd child: a fresh interpreter image that
    survived exec from an owner parent. It self-reports (a) whether any
    descriptor in its OWN fd table still resolves to the authority lock
    file — the empirical close-on-exec observation at the /proc OS
    boundary, absent where the platform exposes no /proc — and (b) that
    it cannot USE the living parent's ownership: its own
    AuthorityOwnerLock.acquire() is refused authority_busy while the
    parent holds the domain. It releases nothing (it never owned) and
    exits on its gate."""
    record: dict[str, Any] = {"child_alive": True, "pid": os.getpid()}

    lock_path = str((state_dir / "authority.lock").resolve())
    inherited = False
    saw_procfs = False
    proc_fd = Path("/proc/self/fd")
    if proc_fd.exists():
        saw_procfs = True
        for fd_link in proc_fd.iterdir():
            try:
                if str(fd_link.readlink()) == lock_path:
                    inherited = True
            except OSError:
                continue
    if saw_procfs:
        record["inherited_lock_fd"] = inherited
    # F-117: the child self-reports its kernel start time (field 22 of
    # /proc/self/stat) so the controller can prove the pid that
    # survives the parent's death is THIS process, not a reused pid.
    self_stat = Path("/proc/self/stat")
    if self_stat.exists():
        try:
            raw = self_stat.read_text(encoding="utf-8")
            fields = raw[raw.rindex(")") + 1 :].split()
            record["starttime"] = int(fields[19])
        except (OSError, ValueError, IndexError):
            pass

    child_lock = AuthorityOwnerLock(state_dir)
    try:
        child_lock.acquire()
        record["child_acquire"] = "acquired"
        child_lock.release()
    except AuthorityBusyError:
        record["child_acquire"] = "busy"
    _write(result_path, record)
    try:
        _wait_gate(release_gate, timeout=120)
    except Exception:  # noqa: BLE001 - the gate is best-effort cleanup
        pass
    os._exit(0)


# ---------------------------------------------------------------------------
# T33: full owner whose runtime is a REAL OS child process
# ---------------------------------------------------------------------------

_CHILD_HOLD_SCRIPT = (
    "import json, sys, time\n"
    "marker_dir, gate, timeout = sys.argv[1], sys.argv[2], float(sys.argv[3])\n"
    "pid = __import__('os').getpid()\n"
    "record = {'pid': pid}\n"
    "try:\n"
    "    raw = open(f'/proc/{pid}/stat').read()\n"
    "    record['starttime'] = int(raw[raw.rindex(')') + 1:].split()[19])\n"
    "except (OSError, ValueError, IndexError):\n"
    "    pass\n"
    # Per-child readiness marker (F-124): the file NAME binds the pid,
    # the CONTENT carries the child's own identity (pid + kernel start
    # time) — the controller matches BOTH against the live process.
    "open(f'{marker_dir}/browser-child-{pid}.pid', 'w').write(json.dumps(record))\n"
    "deadline = time.monotonic() + timeout\n"
    "while not __import__('pathlib').Path(gate).exists():\n"
    "    if time.monotonic() >= deadline:\n"
    "        break\n"
    "    time.sleep(0.1)\n"
)


class _ChildBackedBrowser:
    """F-114: a controlled, subprocess-backed stand-in for SuperBrowser
    injected at the DEPENDENCY boundary (``webwire.session.SuperBrowser``)
    so PRODUCTION SessionManager.start() executes its real decision path —
    attach gate, owned-launch selection, _restore_session. ``start()``
    launches a REAL OS child process (the standing browser-child
    surrogate); ``stop()`` terminates it. Test orchestration in THIS
    process only; no production source is changed."""

    def __init__(self, config: Any) -> None:
        self.config = config
        self._child: Any = None
        self._stopped = False

    async def start(self) -> Any:
        from webwire.envelope import ok_result

        if self._child is not None:
            return ok_result(data={"already_started": True})
        import subprocess as _sp

        marker_dir = Path(self._ww_state_dir)  # noqa: ASYNC240
        self._child = _sp.Popen(  # noqa: S603, ASYNC220 - fixed interpreter + inline hold script
            [
                sys.executable,
                "-c",
                _CHILD_HOLD_SCRIPT,
                str(marker_dir),
                str(self._child_gate),
                "120",
            ],
            cwd=str(Path(__file__).resolve().parent.parent),  # noqa: ASYNC240
        )
        return ok_result(data={"child_pid": self._child.pid})

    async def stop(self) -> Any:
        import subprocess as _sp

        from webwire.envelope import ok_result

        self._stopped = True
        if self._child is not None and self._child.poll() is None:
            self._child.terminate()
            try:
                self._child.wait(timeout=10)
            except _sp.TimeoutExpired:
                self._child.kill()
                self._child.wait(timeout=10)
        return ok_result(data={"stopped": True})

    async def save_session(self, path: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"saved": path})

    async def load_session(self, path: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"loaded": path})


def owner_browser_child(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    child_gate: Path,
    die: bool,
    events_path: Path = None,  # type: ignore[assignment]
) -> int:
    """T33's owner: a full production owner (real Dispatcher, real
    authority, production IPC) whose runtime is a REAL OS child process
    launched through PRODUCTION SessionManager.start() — the attach-gate
    and owned-launch decision are the production code under
    qualification (F-114); only the SuperBrowser dependency is a
    controlled subprocess-backed stand-in. With ``events_path`` set
    (F-118), every actual Dispatcher invocation is logged — the
    controller's execution oracle for "the successor SERVES while the
    orphan lives". The browser-child law (§17.6): the child is not
    authority; a successor must start its OWN child and must never
    attach to a survivor."""
    import asyncio

    import webwire.session as _session_module

    def _build() -> Any:
        from webwire.config import WebWireConfig
        from webwire.dispatcher import Dispatcher
        from webwire.session import SessionManager

        def _child_backed_factory(config: Any) -> "_ChildBackedBrowser":
            browser = _ChildBackedBrowser(config)
            browser._child_gate = child_gate  # noqa: SLF001 - worker-side orchestration
            browser._ww_state_dir = state_dir  # noqa: SLF001 - worker-side orchestration
            return browser

        # F-114: substitute ONLY the dependency; SessionManager.start()
        # below is the untouched production method.
        _session_module.SuperBrowser = _child_backed_factory  # type: ignore[assignment]

        cfg = WebWireConfig(state_dir=state_dir, kill_env_var=None)
        sm = SessionManager(cfg)  # production default config: LAUNCH (owned)
        dispatcher = Dispatcher(cfg, session_manager=sm, enable_ipc=True)  # type: ignore[arg-type]
        from types import SimpleNamespace

        dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
            lambda sb: setattr(
                dispatcher,
                "_m5_stack",
                SimpleNamespace(
                    read_broker=_StubSB(),
                    post_text_executor=object(),
                    reply_executor=object(),
                    quote_executor=object(),
                    delete_executor=object(),
                    effect_executor=object(),
                    media_executor=object(),
                ),
            )
        )
        return dispatcher

    async def _run() -> int:
        dispatcher = _build()
        started = await dispatcher.start()
        if not started.ok:
            _write(
                result_path,
                {"started": False, "error": getattr(started.error, "message", str(started))},
            )
            return 5
        if events_path is not None:
            original_invoke = dispatcher._invoke_admitted

            async def _logging_invoke(name: str, payload: dict) -> Any:
                with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                    fh.write(json.dumps({"invoke": name}) + "\n")
                return await original_invoke(name, payload)

            dispatcher._ipc_server._invoke = _logging_invoke
        # Actor identity, established the way whoami would in
        # production (M5 migrated writes require a resolved handle).
        # Worker-side orchestration AFTER production start() has run —
        # the startup decision under qualification (F-114) is untouched.
        dispatcher._session.set_resolved_handle("@owner")
        session = dispatcher._authority_session
        browser = dispatcher._session._sb
        _write(
            result_path,
            {
                "started": True,
                "instance_id": session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
                "browser_child_pid": browser._child.pid,
                "ownership_mode": dispatcher._session.ownership,
                "session_state": dispatcher._session.session_loaded_state,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 240.0)
        if die:
            # Owner dies WITHOUT stopping its child: the child survives
            # as an orphan exactly like a launched browser would.
            os._exit(9)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# T65: serving owner for supported-fault qualification (invoke-counted)
# ---------------------------------------------------------------------------


class _ServingPostPort:
    """The post-text DOM port shape, fully serving: fill/verify ok, and
    a click_submit that runs the production commit gate (durable
    RESERVED), then completes — no barrier, no death. Used by T65 where
    the mutation must actually finish so the owner's mutation
    capability after a fault is observable. CONTROLLED ADAPTER (F-121):
    no external post occurs — the external-effect boundary is this
    adapter; the qualification claim stops at production admission,
    durable commit, and the terminal effect row. Paired with
    _DieEvidence, which likewise supplies predetermined verification
    data rather than observing a real surface."""

    def __init__(self) -> None:
        self.composer_text = ""

    async def fill_composer(self, text: str) -> Any:
        self.composer_text = text
        from webwire.envelope import ok_result

        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"count": 0})

    async def close_composer(self) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"cleanup": "closed"})

    async def attach_media(self, image_path: str) -> Any:
        raise AssertionError("plain post must not attach media")

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"opened": True})

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> Any:
        from webwire.envelope import ok_result

        return ok_result(data={"opened": True})

    async def click_submit(
        self, *, _commit_gate, _precommit_check, _expected_text, _expected_attachments
    ) -> Any:
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        from webwire.envelope import ok_result

        return ok_result(data={"post_id": "99", "post_url": "https://x.com/owner/status/99"})


def full_owner_serving(
    result_path: Path,
    state_dir: Path,
    stop_gate: Path,
    events_path: Path,
    fault_ledger_once: str = "",
    churn_marker: str = "",
) -> int:
    """T65's serving owner: a full production owner with the REAL M5
    post-text executor stack (previews mint real tokens; confirms run
    the durable commit gate and complete) and production IPC LIVE,
    invoke-logged per actual Dispatcher invocation. With
    ``fault_ledger_once`` set to a marker path, the M5 attempt-ledger
    append raises one injected OSError(ENOSPC) — a supported I/O fault —
    exactly once, then passes through untouched. With ``churn_marker``
    set, the OWNER PROCESS ITSELF (F-113/RV03's exact hazard) opens and
    closes a SECOND descriptor to authority.lock 40 times while the
    private owner handle stays held, then writes the marker — a
    process-associated record-lock implementation would release here;
    the qualified open-file-description primitive must not."""
    import asyncio

    async def _run() -> int:
        from types import SimpleNamespace

        from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
        from webwire.safety.m5_actor_bound_post_executor import M5ActorBoundPostTextExecutor
        from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
        from webwire.safety.scoped_authority import ScopedAuthorityBroker

        dispatcher = _build_dispatcher(state_dir, state_dir)
        # Qualification bucket: T65 drives several post previews/confirms
        # against ONE owner process; the production default (3/hour) would
        # deny the later mutations for rate reasons unrelated to the law
        # under test. Worker-side orchestration only.
        from webwire.safety.token_bucket import BucketLimits, TokenBucket

        wide_bucket = TokenBucket(
            limits={
                "post": BucketLimits(max_count=100, window_seconds=3600),
                "_global": BucketLimits(max_count=1000, window_seconds=300),
            }
        )
        dispatcher._bucket = wide_bucket
        # The kernel captured its own reference at construction time
        # (write_kernel.py stores token_bucket on self._bucket) — widen
        # BOTH, or the policy gate keeps the production 3/hour limits.
        dispatcher._write_kernel._bucket = wide_bucket
        started = await dispatcher.start()
        if not started.ok:
            _write(result_path, {"started": False})
            return 5

        if churn_marker:
            # F-113: RV03's hazard executes INSIDE the lock-owning
            # process — a second descriptor to the very lock file (the
            # canonical authority-domain rendezvous), opened and closed
            # 40 times while the private owner handle stays held. A
            # process-associated record-lock primitive (POSIX fcntl
            # record locks) RELEASES on any same-process descriptor
            # close; the qualified primitive must survive.
            owner_lock_path = state_dir / "authority.lock"
            for _cycle in range(40):
                fd = os.open(owner_lock_path, os.O_RDWR)
                os.close(fd)
            _write(Path(churn_marker), {"owner_local_churn_done": True})

        if fault_ledger_once:
            # Supported I/O fault at the FIRST durable append of the next
            # mutation (the confirm's durable reservation seam — the same
            # append commit_gateway.py drives through the effect ledger).
            # Instance-level patch: one owner, one ledger, no class-wide
            # mutation.
            marker = Path(fault_ledger_once)
            ledger = dispatcher._m5_gateway._ledger
            original_append = ledger.append_durable

            def _faulting_append(*args: Any, **kwargs: Any) -> Any:
                if not marker.exists():
                    marker.write_text("armed", encoding="utf-8")
                    raise OSError(28, "injected ENOSPC: supported I/O fault")
                return original_append(*args, **kwargs)

            ledger.append_durable = _faulting_append  # type: ignore[method-assign]

        port = _ServingPostPort()
        scoped = ScopedAuthorityBroker(port, dispatcher._m5_gateway, policies=DEFAULT_EFFECT_POLICIES)
        runtime = M5ExecutionRuntime(
            scoped_authority=scoped,
            commit_gateway=dispatcher._m5_gateway,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        dispatcher._m5_stack = SimpleNamespace(
            read_broker=_StubSB(),
            post_text_executor=M5ActorBoundPostTextExecutor(runtime=runtime, evidence_reader=_DieEvidence()),
        )

        original_invoke = dispatcher._invoke_admitted

        async def _logging_invoke(name: str, payload: dict) -> Any:
            with open(events_path, "a", encoding="utf-8") as fh:  # noqa: ASYNC230 — worker-side orchestration
                fh.write(json.dumps({"invoke": name}) + "\n")
            return await original_invoke(name, payload)

        dispatcher._ipc_server._invoke = _logging_invoke
        _write(
            result_path,
            {
                "started": True,
                "instance_id": dispatcher._authority_session.authority_instance_id,
                "endpoint": str(dispatcher._ipc_transport.endpoint_path or ""),
                "build_id": dispatcher._ipc_server._runtime_build_id,
            },
        )
        await asyncio.get_event_loop().run_in_executor(None, _wait_gate, stop_gate, 240.0)
        await dispatcher.stop()
        return 0

    return asyncio.run(_run())


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
            Path(raw[7]) if len(raw) > 7 else None,
        )
    if scenario == "ipc-raw":
        return ipc_raw(result_path, Path(raw[0]), raw[1], raw[2])
    if scenario == "fork-child":
        return fork_child(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "lock-race":
        return lock_race(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]))
    if scenario == "full-owner-blocked":
        return full_owner_blocked(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]), Path(raw[3]), raw[4])
    if scenario == "full-owner-media-block":
        return full_owner_media_block(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]), Path(raw[3]))
    if scenario == "ingest-crash":
        return ingest_crash(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "ipc-flood":
        return ipc_flood(
            result_path,
            Path(raw[0]),
            raw[1],
            raw[2],
            raw[3],
            Path(raw[4]),
            int(raw[5]),
            raw[6] == "dup",
            raw[7] if len(raw) > 7 else "",
        )
    if scenario == "full-owner-m5-crash-at":
        return full_owner_m5_crash_at(result_path, Path(raw[0]), Path(raw[1]), raw[2])
    if scenario == "full-owner-t16":
        return full_owner_t16(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]), Path(raw[3]))
    if scenario == "full-owner-observing":
        return full_owner_observing(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]))
    if scenario == "recon-append-crash":
        return recon_append_crash(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "full-owner-counting":
        return full_owner_counting(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]))
    if scenario == "full-owner-m5-serving":
        return full_owner_m5_serving(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "full-owner-tablesat":
        return full_owner_tablesat(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]), Path(raw[3]))
    if scenario == "owner-spawn-child":
        return owner_spawn_child(result_path, Path(raw[0]), Path(raw[1]), Path(raw[2]))
    if scenario == "child-exec":
        return child_exec(result_path, Path(raw[0]), Path(raw[1]))
    if scenario == "owner-browser-child":
        return owner_browser_child(
            result_path,
            Path(raw[0]),
            Path(raw[1]),
            Path(raw[2]),
            raw[3] == "die",
            Path(raw[4]) if len(raw) > 4 else None,
        )
    if scenario == "full-owner-serving":
        return full_owner_serving(
            result_path,
            Path(raw[0]),
            Path(raw[1]),
            Path(raw[2]),
            raw[3] if len(raw) > 3 else "",
            raw[4] if len(raw) > 4 else "",
        )
    print(f"unknown scenario {scenario}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
