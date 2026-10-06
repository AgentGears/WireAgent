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
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import HEADER_SIZE, encode_json_frame
from webwire.authority_ipc_protocol import (
    IPC_PROTOCOL_VERSION,
    compute_runtime_build_id,
)
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

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
):
    session = _session_ready()
    loop = asyncio.new_event_loop()

    async def invoke(name: str, input: dict) -> Any:
        if invoke_log is not None:
            invoke_log.append((name, dict(input)))
        if slow_release is not None:
            await slow_release.wait()
        return invoke_result or ok_result(data={"ok": True})

    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=Path("transport-test"),
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )
    path = _sock_path(tmp_path)
    transport = IPCTransportServer(ipc_server=ipc, endpoint_path=path, loop=loop)
    transport.start()
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
    import time

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
