"""M7 Layer 4 Windows named-pipe transport tests (F-64).

Real named-pipe qualification on Windows: the DACL construction (the
9-arg BuildSecurityDescriptorW ABI), the accept loop's next-instance
behavior, the local pipe client, real frame I/O over ReadFile/WriteFile,
and the shutdown/successor lifecycle. These run ONLY on win32; the
POSIX equivalents live in tests/test_m7_layer4_transport.py.
"""

from __future__ import annotations

import asyncio
import secrets
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import HEADER_SIZE
from webwire.authority_ipc_protocol import (
    IPC_PROTOCOL_VERSION,
    compute_runtime_build_id,
)
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows named-pipe transport")

BUILD_ID = compute_runtime_build_id()


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("windows-transport-test"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _server_with_transport(tmp_path: Path, invoke_log: list | None = None):
    session = _session_ready()
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    loop_thread = threading.Thread(target=_run_loop, daemon=True)
    loop_thread.start()

    async def invoke(name: str, input: dict) -> Any:
        if invoke_log is not None:
            invoke_log.append((name, dict(input)))
        return ok_result(data={"ok": True})

    authority_domain = tmp_path
    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=authority_domain,
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=authority_domain, loop=loop)
    transport.start()
    return session, ipc, transport, loop, str(transport.endpoint_path or "")


def _envelope(operation: str, payload: Any, *, instance_id: str) -> dict[str, Any]:
    return {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "request_id": secrets.token_hex(16),
        "authority_instance_id": instance_id,
        "runtime_build_id": BUILD_ID,
    }


def _teardown(transport, loop) -> None:
    transport.stop()
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.2)
    loop.close()


# ---------------------------------------------------------------------------
# Endpoint/DACL construction (the old failure point)
# ---------------------------------------------------------------------------


def test_windows_endpoint_binds_with_current_user_dacl(tmp_path: Path) -> None:
    """The DACL construction (token → SID → 9-arg BuildSecurityDescriptorW
    → SECURITY_ATTRIBUTES → CreateNamedPipeW) completes without error —
    the exact path that failed before the argtypes repair."""
    from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint

    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    assert endpoint.path.startswith(r"\\.\pipe\webwire-authority-")
    try:
        endpoint.bind()
        assert endpoint.path
    finally:
        endpoint.close()


def test_windows_pipe_name_is_stable_per_domain(tmp_path: Path) -> None:
    """The rendezvous identifier derives deterministically from the
    canonical authority domain."""
    from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint

    assert WindowsNamedPipeEndpoint(tmp_path).path == WindowsNamedPipeEndpoint(tmp_path).path
    assert WindowsNamedPipeEndpoint(tmp_path).path != WindowsNamedPipeEndpoint(
        tmp_path / "other"
    ).path


# ---------------------------------------------------------------------------
# Real transport roundtrips over named pipes
# ---------------------------------------------------------------------------


def test_windows_real_transport_roundtrip(tmp_path: Path) -> None:
    """A real client connects over the named pipe, receives hello, sends
    a request, and gets a response through ReadFile/WriteFile framing."""
    log: list = []
    session, ipc, transport, loop, path = _server_with_transport(tmp_path, invoke_log=log)
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
        assert log == [("read", {"post_url": "https://x.com/x/1"})]
        client.close()
    finally:
        _teardown(transport, loop)


def test_windows_sequential_clients_next_instance(tmp_path: Path) -> None:
    """The accept loop creates the NEXT pipe instance after each
    connection — a second client AFTER the first disconnected still finds
    a rendezvous (the old single-instance bug)."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        for _round in range(3):
            client = IPCClient(path, expected_build_id=BUILD_ID)
            client.connect()
            response = client.request(
                _envelope("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
            )
            assert response["ok"] is True
            client.close()
    finally:
        _teardown(transport, loop)


def test_windows_concurrent_clients_two_instances(tmp_path: Path) -> None:
    """Two clients hold connections SIMULTANEOUSLY (one pipe instance
    each) and both complete full roundtrips."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    results: list[Any] = []

    def _one_roundtrip() -> None:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        response = client.request(
            _envelope("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
        )
        results.append(response)
        client.close()

    try:
        threads = [threading.Thread(target=_one_roundtrip) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert len(results) == 2
        assert all(r["ok"] is True for r in results)
    finally:
        _teardown(transport, loop)


def test_windows_oversized_request_refused(tmp_path: Path) -> None:
    """A frame announcing >64KiB is refused from the header, BEFORE the
    body is read (same law as POSIX)."""
    import struct

    from webwire.authority_ipc_endpoint import connect_local_stream

    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    try:
        conn = connect_local_stream(path)
        # Read the hello first.
        header_bytes = b""
        while len(header_bytes) < HEADER_SIZE:
            chunk = conn.recv(HEADER_SIZE - len(header_bytes))
            if not chunk:
                raise AssertionError("connection closed before hello")
            header_bytes += chunk
        (hello_len,) = struct.unpack(">Q", header_bytes)
        body = b""
        while len(body) < hello_len:
            body += conn.recv(hello_len - len(body))

        # Announce 100KiB (over the 64KiB request ceiling).
        conn.send(struct.pack(">Q", 100 * 1024))
        # The server closes without reading the body: broken pipe / EOF.
        with pytest.raises((ConnectionError, OSError)):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                conn.recv(1)
                time.sleep(0.05)
        conn.close()
    finally:
        _teardown(transport, loop)


def test_windows_idle_client_shutdown_successor_binds(tmp_path: Path) -> None:
    """Drain closes a stalled client's connection (CancelIoEx), and once
    all handles are closed a SUCCESSOR endpoint can bind the same pipe
    name (FILE_FLAG_FIRST_PIPE_INSTANCE succeeds on a free name)."""
    session, ipc, transport, loop, path = _server_with_transport(tmp_path)
    client = IPCClient(path, expected_build_id=BUILD_ID)
    client.connect()
    sock = client._sock
    try:
        # Drain with the client idle (stalled before its request frame).
        transport.stop()

        # The stalled client's pending read is released with an error.
        with pytest.raises((ConnectionError, OSError)):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                sock.recv(1)
                time.sleep(0.05)
    finally:
        client.close()
        _teardown(transport, loop)

    # The successor binds the same rendezvous name.
    from webwire.authority_ipc_endpoint import create_endpoint

    successor = create_endpoint(tmp_path)
    assert successor.path == path
    successor.bind()
    successor.close()
