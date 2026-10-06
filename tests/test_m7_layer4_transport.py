"""M7 Layer 4 transport tests — real POSIX socket connections (F-60).

These tests exercise the ACTUAL transport: a real Unix-domain socket
(or Windows named pipe), a real accept loop, real frame I/O, real hello
exchange, and real client disconnect behavior. They replace the earlier
in-process-only pipeline tests as the transport qualification.

The disconnect tests directly prove the conservative disconnect law:
client socket closure does NOT cancel the admitted owner task.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import struct
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import HEADER_SIZE, encode_json_frame
from webwire.authority_ipc_protocol import (
    IPC_MAX_RESPONSE_BYTES,
    IPC_PROTOCOL_VERSION,
    IPC_SUPPORTED_OPERATIONS,
    AuthorityHello,
    compute_runtime_build_id,
)
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.config import WebWireConfig
from webwire.envelope import ok_result
from webwire.safety.kill_switch import KillSwitch

# POSIX-only: AF_UNIX sockets. Windows named-pipe transport qualification
# is Layer 8; the security pipeline is qualified independently on both
# platforms via tests/test_m7_layer4_ipc.py.
pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets are POSIX-only")


BUILD_ID = compute_runtime_build_id()


def _sock_path(tmp_path: Path) -> str:
    return str(tmp_path / "authority.sock")


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("transport-test"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _server_with_transport(
    tmp_path: Path,
    invoke_result: Any = None,
    invoke_log: list | None = None,
    slow_release: asyncio.Event | None = None,
    max_concurrent: int = 4,
    kill_probe: Any = None,
    recovery_probe: Any = None,
):
    session = _session_ready()
    loop = asyncio.new_event_loop()

    import threading

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True)
    loop_thread.start()

    async def invoke(name: str, input: dict) -> Any:
        if invoke_log is not None:
            invoke_log.append((name, dict(input)))
        if slow_release is not None:
            await slow_release.wait()
        return invoke_result or ok_result(data={"ok": True})

    authority_domain = tmp_path  # F-66: the qualified endpoint derives from this
    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=authority_domain,
        invoke=invoke,
        runtime_build_id=BUILD_ID,
        kill_probe=kill_probe,
        recovery_probe=recovery_probe,
    )
    transport = IPCTransportServer(
        ipc_server=ipc,
        authority_domain=authority_domain,
        loop=loop,
        max_concurrent=max_concurrent,
    )
    transport.start()
    path = transport.endpoint_path or ""
    return session, ipc, transport, loop, path


def _envelope(
    operation: str,
    payload: Any,
    *,
    instance_id: str,
    build_id: str | None = BUILD_ID,
    protocol: int = IPC_PROTOCOL_VERSION,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "protocol_version": protocol,
        "operation": operation,
        "payload": payload,
        "request_id": secrets.token_hex(16),
        "authority_instance_id": instance_id,
        "runtime_build_id": build_id,
    }
    return body


def _teardown(transport, loop) -> None:
    transport.stop()
    loop.call_soon_threadsafe(loop.stop)
    import time

    time.sleep(0.2)
    loop.close()


# ---------------------------------------------------------------------------
# Real transport roundtrip
# ---------------------------------------------------------------------------


async def test_real_transport_roundtrip(tmp_path: Path) -> None:
    """A real client connects, receives hello, sends a request, gets a
    response through the ACTUAL socket."""
    log: list = []
    session, ipc, transport, loop, path = _server_with_transport(
        tmp_path, invoke_result=ok_result(data={"posts": [{"id": "1"}]}), invoke_log=log
    )
    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        hello = client.connect()
        assert hello.protocol_version == IPC_PROTOCOL_VERSION
        assert hello.runtime_build_id == BUILD_ID
        assert hello.authority_instance_id == session.authority_instance_id

        response = client.request(
            _envelope("read", {"post_url": "https://x.com/x/1"}, instance_id=session.authority_instance_id)
        )
        assert response["ok"] is True
        assert response["data"]["posts"][0]["id"] == "1"
        assert log == [("read", {"post_url": "https://x.com/x/1"})]
        client.close()
    finally:
        _teardown(transport, loop)


async def test_real_transport_stale_instance_rejected(tmp_path: Path) -> None:
    """Stale instance through a REAL socket: the client gets the frozen
    stale_authority_instance error, and the Dispatcher is never reached."""
    log: list = []
    session, ipc, transport, loop, path = _server_with_transport(tmp_path, invoke_log=log)
    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        stale = "f" * 64
        response = client.request(_envelope("read", {"post_url": "u"}, instance_id=stale))
        assert response["ok"] is False
        assert response["error"]["code"] == "stale_authority_instance"
        assert log == []
        client.close()
    finally:
        _teardown(transport, loop)


async def test_real_transport_build_mismatch_client_refuses(tmp_path: Path) -> None:
    """A client with a different build identity refuses BEFORE sending a
    request (the hello verification catches it)."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        client = IPCClient(path, expected_build_id="0" * 64)
        with pytest.raises(ValueError, match="build mismatch"):
            client.connect()
    finally:
        _teardown(transport, loop)


async def test_real_transport_oversized_request_refused(tmp_path: Path) -> None:
    """A frame announcing >64KiB is refused from the header, BEFORE the
    body is read or any JSON allocation occurs."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        # Read hello.
        hdr = b""
        while len(hdr) < HEADER_SIZE:
            hdr += sock.recv(HEADER_SIZE - len(hdr))
        (hello_len,) = struct.unpack(">Q", hdr)
        hello_body = b""
        while len(hello_body) < hello_len:
            hello_body += sock.recv(hello_len - len(hello_body))

        # Send a header announcing 100KiB (over the 64KiB request ceiling).
        evil = struct.pack(">Q", 100 * 1024)
        sock.send(evil)
        # The server should close the connection (refuse without reading).
        sock.settimeout(5)
        result = sock.recv(1)
        assert result == b"", "server should close on oversized announcement"
        sock.close()
    finally:
        _teardown(transport, loop)


# ---------------------------------------------------------------------------
# Real disconnect qualification (F-60: the conservative disconnect law)
# ---------------------------------------------------------------------------


async def test_real_disconnect_does_not_cancel_admitted_work(tmp_path: Path) -> None:
    """THE reviewer-specified disconnect sequence through a REAL socket:
    client connects, sends a valid read, the owner admits and blocks, the
    client socket is FORCIBLY closed, the owner invocation continues to
    its terminal boundary, admission returns to 0."""
    import threading

    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    release = asyncio.Event()

    # Patch the invoke to block on the event.

    async def blocking_invoke(name: str, input: dict) -> Any:
        await loop.run_in_executor(None, _wait_event, release)
        return ok_result(data={"done": True})

    ipc._invoke = blocking_invoke

    completed: list = []

    def _watch() -> None:
        # Wait for the admission to appear.
        import time

        deadline = time.monotonic() + 5
        while session.active_work == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        # Force the client socket closed.
        sock.close()
        # Release the owner work.
        loop.call_soon_threadsafe(release.set)
        completed.append("disconnected_and_released")

    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        # Keep a reference to the raw socket for the force-close.
        sock = client._sock
        assert sock is not None

        envelope = _envelope("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
        frame = encode_json_frame(envelope)

        # Send the request in a thread (the owner will block).
        import threading

        def send_and_recv() -> Any:
            sock.send(frame)
            try:
                resp = sock.recv(1)
                return resp
            except OSError:
                return b""

        sender = threading.Thread(target=send_and_recv)
        sender.start()

        # Watch for admission, then disconnect.
        watcher = threading.Thread(target=_watch)
        watcher.start()

        # Wait for the disconnect + release to complete.
        watcher.join(timeout=10)
        sender.join(timeout=10)

        assert completed == ["disconnected_and_released"]
        # The owner invocation completed (not cancelled): admission is 0.
        for _poll in range(100):  # bounded poll, max 5s
            if session.active_work == 0:
                break
            await asyncio.sleep(0.05)
        assert session.active_work == 0, "the admitted owner work completed despite client disconnect"
    finally:
        _teardown(transport, loop)


def _wait_event(event: asyncio.Event) -> None:
    """Block until the event is set (called from executor thread)."""
    deadline = time.monotonic() + 10
    while not event.is_set() and time.monotonic() < deadline:
        time.sleep(0.05)


async def test_disconnect_before_request_no_admission(tmp_path: Path) -> None:
    """A client that connects, reads hello, then disconnects WITHOUT
    sending a request: no owner admission, no Dispatcher invocation."""
    log: list = []
    session, ipc, transport, loop, path = _server_with_transport(tmp_path, invoke_log=log)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        # Read hello.
        hdr = b""
        while len(hdr) < HEADER_SIZE:
            hdr += sock.recv(HEADER_SIZE - len(hdr))
        (hello_len,) = struct.unpack(">Q", hdr)
        hello_body = b""
        while len(hello_body) < hello_len:
            hello_body += sock.recv(hello_len - len(hello_body))
        # Disconnect without sending anything.
        sock.close()
        import time

        time.sleep(0.2)  # let the handler see the close  # noqa: ASYNC251
        assert session.active_work == 0
        assert log == [], "no Dispatcher invocation"
    finally:
        _teardown(transport, loop)


# ---------------------------------------------------------------------------
# F-62: saturation — bounded handler slots refuse, not queue
# ---------------------------------------------------------------------------


async def test_saturation_third_connection_refused_without_hello(tmp_path: Path) -> None:
    """Capacity 2: two admitted requests hold both handler slots; a THIRD
    connection is refused at accept time — closed without ever receiving a
    hello. No unlimited waiters, no queue growth. After release, the two
    held requests complete normally."""
    release = asyncio.Event()
    session, ipc, transport, loop, path = _server_with_transport(
        tmp_path, slow_release=release, max_concurrent=2
    )
    results: list[Any] = []

    def _held_request() -> None:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        response = client.request(
            _envelope("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
        )
        results.append(response)
        client.close()

    first = threading.Thread(target=_held_request)
    second = threading.Thread(target=_held_request)
    first.start()
    second.start()
    try:
        # Wait until BOTH requests are admitted (each admission implies its
        # handler slot is held for the whole connection). The admission
        # counter is mutated on the owner loop's thread — poll it.
        deadline = time.monotonic() + 5
        while ipc.active_requests < 2 and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert ipc.active_requests == 2, "both held requests must be admitted"

        # The third connection: refused WITHOUT a hello (closed at accept).
        with pytest.raises(ConnectionError):
            IPCClient(path, expected_build_id=BUILD_ID).connect()

        # Release: the two held requests complete.
        loop.call_soon_threadsafe(release.set)
        first.join(timeout=10)
        second.join(timeout=10)
        assert len(results) == 2
        assert all(r["ok"] is True for r in results)
    finally:
        _teardown(transport, loop)


# ---------------------------------------------------------------------------
# F-65: the abnormal §10.3 wire states are PRODUCIBLE from live signals
# ---------------------------------------------------------------------------


async def test_wire_state_killed_via_real_kill_switch(tmp_path: Path) -> None:
    """A tripped KillSwitch (the SAME object the enforcement path consults)
    makes the hello carry state=killed on the wire."""
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path / "kill-domain"))
    session, ipc, transport, loop, path = _server_with_transport(
        tmp_path, kill_probe=kill.tripped
    )
    try:
        kill.trip()
        client = IPCClient(path, expected_build_id=BUILD_ID)
        hello = client.connect()
        assert hello.lifecycle_state == "killed"
        client.close()
        kill.reset()
    finally:
        _teardown(transport, loop)


async def test_wire_state_recovery_unavailable_via_probe(tmp_path: Path) -> None:
    """A recovery probe reporting unavailable makes the hello carry
    state=recovery_unavailable on the wire (the guard's status().available
    degrades to False when composite recovery truth cannot be held)."""
    session, ipc, transport, loop, path = _server_with_transport(
        tmp_path, recovery_probe=lambda: True
    )
    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        hello = client.connect()
        assert hello.lifecycle_state == "recovery_unavailable"
        client.close()
    finally:
        _teardown(transport, loop)


# ---------------------------------------------------------------------------
# F-67: malicious-server regressions — the CLIENT refuses oversized frames
# before body allocation (bounded receive on the client side)
# ---------------------------------------------------------------------------


def _fake_hello_frame() -> bytes:
    hello = AuthorityHello(
        protocol_version=IPC_PROTOCOL_VERSION,
        runtime_version="3.11.0",
        runtime_build_id=BUILD_ID,
        authority_instance_id="a" * 64,
        authority_domain="/fake",
        supported_operations=IPC_SUPPORTED_OPERATIONS,
        lifecycle_state="ready",
    )
    return encode_json_frame(hello.to_dict(), is_response=True)


def _serve_one_connection(server_sock: socket.socket, frames: list[bytes]) -> None:
    """Accept one client, send the prepared frames, hold the connection."""
    server_sock.settimeout(10)
    conn, _ = server_sock.accept()
    try:
        for frame in frames:
            conn.sendall(frame)
        conn.settimeout(10)
        try:
            conn.recv(1)  # hold open until the client closes
        except OSError:
            pass
    finally:
        conn.close()


async def test_client_refuses_oversized_hello_before_body_read(tmp_path: Path) -> None:
    """A malicious server announcing an over-ceiling hello length: the
    client refuses from the HEADER, before allocating/reading the body."""
    evil = struct.pack(">Q", IPC_MAX_RESPONSE_BYTES + 1)
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    fake_path = str(tmp_path / "evil.sock")
    server_sock.bind(fake_path)
    server_sock.listen(1)
    try:
        thread = threading.Thread(target=_serve_one_connection, args=(server_sock, [evil]))
        thread.start()

        client = IPCClient(fake_path, expected_build_id=BUILD_ID)
        with pytest.raises(ValueError, match="exceeds the .*-byte ceiling"):
            client.connect()
        assert client._sock is None, "the client must close after refusing"
        thread.join(timeout=10)
    finally:
        server_sock.close()


async def test_client_refuses_oversized_response_before_body_read(tmp_path: Path) -> None:
    """A malicious server: valid hello, then an over-ceiling RESPONSE
    announcement. The client's request() refuses from the header."""
    server_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    fake_path = str(tmp_path / "evil2.sock")
    server_sock.bind(fake_path)
    server_sock.listen(1)

    def _hello_then_evil() -> None:
        server_sock.settimeout(10)
        conn, _ = server_sock.accept()
        try:
            conn.sendall(_fake_hello_frame())
            # Swallow the client's request frame (bounded).
            hdr = b""
            while len(hdr) < HEADER_SIZE:
                chunk = conn.recv(HEADER_SIZE - len(hdr))
                if not chunk:
                    return
                hdr += chunk
            (length,) = struct.unpack(">Q", hdr)
            body = b""
            while len(body) < length:
                chunk = conn.recv(length - len(body))
                if not chunk:
                    return
                body += chunk
            # Announce an over-ceiling response.
            conn.sendall(struct.pack(">Q", IPC_MAX_RESPONSE_BYTES + 1))
            conn.settimeout(10)
            try:
                conn.recv(1)
            except OSError:
                pass
        finally:
            conn.close()

    try:
        thread = threading.Thread(target=_hello_then_evil)
        thread.start()

        client = IPCClient(fake_path, expected_build_id=BUILD_ID)
        client.connect()
        envelope = _envelope("read", {"post_url": "u"}, instance_id="a" * 64)
        with pytest.raises(ValueError, match="exceeds the .*-byte ceiling"):
            client.request(envelope)
        client.close()
        thread.join(timeout=10)
    finally:
        server_sock.close()


# ---------------------------------------------------------------------------
# F-70: SO_PEERCRED peer-identity enforcement (Linux)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="SO_PEERCRED is Linux")
async def test_foreign_peer_uid_rejected_before_hello_listener_survives(
    tmp_path: Path, monkeypatch
) -> None:
    """A connection whose kernel-reported uid is not the owner's effective
    uid is closed BEFORE the hello — and the listener SURVIVES the
    rejection (a rejected peer is not a listener failure); a same-uid
    client immediately after gets full service."""
    import webwire.authority_ipc_endpoint as endpoint_module

    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        real = endpoint_module._expected_peer_uid()
        assert real is not None

        # Phase 1: expect a foreign uid → the peer is rejected pre-hello.
        monkeypatch.setattr(endpoint_module, "_expected_peer_uid", lambda: real + 1)
        with pytest.raises(ConnectionError):
            IPCClient(path, expected_build_id=BUILD_ID).connect()

        # Phase 2: same-uid → the SAME listener serves a full roundtrip.
        monkeypatch.setattr(endpoint_module, "_expected_peer_uid", lambda: real)
        client = IPCClient(path, expected_build_id=BUILD_ID)
        hello = client.connect()
        assert hello.lifecycle_state == "ready"
        response = client.request(
            _envelope("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
        )
        assert response["ok"] is True
        client.close()
    finally:
        _teardown(transport, loop)


# ---------------------------------------------------------------------------
# Idle-client shutdown: drain closes the stalled handler, the rendezvous
# disappears, and a successor owner can bind the same path
# ---------------------------------------------------------------------------


async def test_idle_client_shutdown_successor_binds(tmp_path: Path) -> None:
    """The reviewer-specified shutdown sequence with an IDLE client: the
    client connects, reads the hello, then stalls before sending any
    frame. Drain (transport.stop) closes the stalled connection — the
    handler exits — the rendezvous pathname is ABSENT, and a SUCCESSOR
    endpoint can bind the same path immediately."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(path)
        # Read the hello, then stall (send nothing).
        hdr = b""
        while len(hdr) < HEADER_SIZE:
            hdr += sock.recv(HEADER_SIZE - len(hdr))
        (hello_len,) = struct.unpack(">Q", hdr)
        body = b""
        while len(body) < hello_len:
            body += sock.recv(hello_len - len(body))

        # Drain: close connections first, then the endpoint (unlinks).
        transport.stop()

        # The idle client sees EOF — its handler exited.
        sock.settimeout(5)
        assert sock.recv(1) == b"", "the stalled handler must be closed at drain"
        sock.close()

        # The rendezvous pathname is absent.
        import os

        assert not os.path.exists(path), "endpoint pathname must be gone after stop"  # noqa: ASYNC240

        # A successor owner binds the same path without interference.
        from webwire.authority_ipc_endpoint import create_endpoint

        successor = create_endpoint(tmp_path)
        successor.bind()
        try:
            assert successor.path == path
        finally:
            successor.close()
    finally:
        _teardown(transport, loop)
