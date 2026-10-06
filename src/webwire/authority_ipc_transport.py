"""M7 Layer 4 — the operational POSIX IPC transport.

A real accept loop, bounded connection handling, and frame I/O over
Unix-domain sockets (F-60). One request per connection (the reviewer's
recommended simplification): connect → hello → request → response → close.

The transport is deliberately thread-based (not asyncio-socket) so the
same code path works on both POSIX and Windows named pipes without an
asyncio event-loop integration gap. The security pipeline
(``AuthorityIPCServer.process_request``) remains async and is invoked
from the handler thread via the event loop.

Bound enforcement (F-62): connection-handler slots are bounded BEFORE
task creation — a listener thread accepts only when a handler slot is
free; saturation refuses the connection immediately rather than queueing
unlimited waiters.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
from typing import Any, Optional

from webwire.authority_ipc_framing import HEADER_SIZE, encode_json_frame
from webwire.authority_ipc_protocol import (
    IPC_PROTOCOL_VERSION,
    AuthorityHello,
)
from webwire.authority_ipc_server import AuthorityIPCServer

logger = logging.getLogger(__name__)

__all__ = ["IPCTransportServer", "IPCClient", "connect_and_request"]


def _read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes from a socket or raise ConnectionError."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:
            raise ConnectionError(f"connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def _write_all(sock: socket.socket, data: bytes) -> None:
    """Write all bytes to a socket or raise ConnectionError."""
    sent = 0
    while sent < len(data):
        n = sock.send(data[sent:])
        if n == 0:
            raise ConnectionError("connection closed during write")
        sent += n


class IPCTransportServer:
    """The operational transport: a real accept loop over the endpoint.

    ``start()`` binds the endpoint and begins the bounded listener.
    ``stop()`` closes the listener and endpoint. Between them, real
    clients can connect, exchange a hello, send one framed request, and
    receive one framed response.
    """

    def __init__(
        self,
        *,
        ipc_server: AuthorityIPCServer,
        endpoint_path: str,
        loop: asyncio.AbstractEventLoop,
    ) -> None:
        self._ipc = ipc_server
        self._endpoint_path = endpoint_path
        self._loop = loop
        self._listener_socket: Optional[socket.socket] = None
        self._listener_thread: Optional[threading.Thread] = None
        self._handler_slots = threading.Semaphore(4)  # F-62: bounded BEFORE spawn
        self._running = False

    def start(self) -> None:
        """Bind a real Unix-domain listener and start the accept loop."""
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("AF_UNIX sockets are POSIX-only (Windows named-pipe transport is Layer 8)")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(self._endpoint_path)
            import os

            os.chmod(self._endpoint_path, 0o600)
            sock.listen(4)  # bounded backlog
        except OSError as exc:
            sock.close()
            raise RuntimeError(f"transport bind failed on {self._endpoint_path}: {exc!r}") from exc
        self._listener_socket = sock
        self._running = True
        self._listener_thread = threading.Thread(
            target=self._accept_loop, daemon=True, name="webwire-ipc-listener"
        )
        self._listener_thread.start()

    def stop(self) -> None:
        """Stop accepting and close the listener."""
        self._running = False
        if self._listener_socket is not None:
            try:
                self._listener_socket.close()
            finally:
                self._listener_socket = None
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=5)
            self._listener_thread = None

    def _accept_loop(self) -> None:
        """Accept connections; each gets a bounded handler thread."""
        while self._running:
            assert self._listener_socket is not None
            try:
                conn, _ = self._listener_socket.accept()
            except OSError:
                if self._running:
                    logger.warning("IPC accept loop: accept failed", exc_info=True)
                break
            # F-62: acquire a handler slot BEFORE spawning; saturation
            # refuses the connection immediately (no unlimited waiters).
            if not self._handler_slots.acquire(blocking=False):
                try:
                    conn.close()
                except OSError:
                    pass
                continue
            thread = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                daemon=True,
                name="webwire-ipc-handler",
            )
            thread.start()

    def _handle_connection(self, conn: socket.socket) -> None:
        """One connection: hello → one framed request → response → close."""
        try:
            # 1. Send AuthorityHello.
            hello = self._ipc._hello()
            hello_frame = encode_json_frame(hello.to_dict(), is_response=True)
            _write_all(conn, hello_frame)

            # 2. Read exactly one request frame (8-byte header + body).
            header_bytes = _read_exact(conn, HEADER_SIZE)
            (length,) = struct.unpack(">Q", header_bytes)
            from webwire.authority_ipc_protocol import IPC_MAX_FRAME_BYTES, IPC_MAX_REQUEST_BYTES

            # F-62: refuse BOTH over-limit frames and over-limit REQUESTS
            # from the announced header — BEFORE reading the body or any
            # JSON allocation. The transport is the last boundary before
            # bytes hit the pipeline.
            if length > IPC_MAX_FRAME_BYTES or length > IPC_MAX_REQUEST_BYTES:
                # Close without reading the body (the process_request
                # 64KiB check would also catch it, but only after the
                # body bytes were already read from the socket).
                return
            body = _read_exact(conn, length)

            # 3. Dispatch to the async security pipeline.
            from webwire.authority_ipc_framing import FrameHeader

            header = FrameHeader(length=length)
            future = asyncio.run_coroutine_threadsafe(self._ipc.process_request(header, body), self._loop)
            try:
                response_frame = future.result(timeout=300.0)
            except Exception:
                logger.exception("IPC pipeline error")
                response_frame = encode_json_frame(
                    {"ok": False, "error": {"code": "internal", "message": "pipeline error"}},
                    is_response=True,
                )

            # 4. Write exactly one response frame.
            try:
                _write_all(conn, response_frame)
            except (ConnectionError, OSError):
                # F-60: the conservative disconnect law — the client's
                # response is lost but the owner work already completed
                # inside process_request (under session admission).
                pass
        except (ConnectionError, OSError):
            # Client disconnected before the complete request — no owner
            # admission was reached (the pipeline never ran).
            pass
        finally:
            self._handler_slots.release()
            try:
                conn.close()
            except OSError:
                pass


class IPCClient:
    """A minimal IPC client for Layer-4 qualification.

    ``connect()`` reads and validates AuthorityHello (exact protocol,
    exact build). ``request()`` sends one framed request and reads one
    framed response. One request per connection.
    """

    def __init__(self, endpoint_path: str, *, expected_build_id: str) -> None:
        self._path = endpoint_path
        self._expected_build = expected_build_id
        self._sock: Optional[socket.socket] = None
        self._hello: Optional[AuthorityHello] = None

    def connect(self) -> AuthorityHello:
        """Connect, read hello, verify exact protocol + build."""
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("AF_UNIX sockets are POSIX-only")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._path)
        self._sock = sock
        # Read the hello frame.
        header_bytes = _read_exact(sock, HEADER_SIZE)
        (length,) = struct.unpack(">Q", header_bytes)
        body = _read_exact(sock, length)
        import json

        hello_raw = json.loads(body.decode("utf-8"))
        hello = AuthorityHello.from_dict(hello_raw)

        # Exact protocol check.
        if hello.protocol_version != IPC_PROTOCOL_VERSION:
            self.close()
            raise ValueError(
                f"protocol mismatch: server={hello.protocol_version}, client={IPC_PROTOCOL_VERSION}"
            )
        # Exact build check.
        if hello.runtime_build_id != self._expected_build:
            self.close()
            raise ValueError(
                f"build mismatch: server={hello.runtime_build_id[:16]}…, client={self._expected_build[:16]}…"
            )
        self._hello = hello
        return hello

    def request(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Send one framed request, read one framed response."""
        import json

        if self._sock is None:
            raise RuntimeError("not connected")
        frame = encode_json_frame(envelope)
        _write_all(self._sock, frame)
        # Read the response.
        header_bytes = _read_exact(self._sock, HEADER_SIZE)
        (length,) = struct.unpack(">Q", header_bytes)
        body = _read_exact(self._sock, length)
        return json.loads(body.decode("utf-8"))

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "IPCClient":
        self.connect()
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()


def connect_and_request(
    endpoint_path: str,
    *,
    expected_build_id: str,
    envelope: dict[str, Any],
) -> dict[str, Any]:
    """One-shot: connect → hello → request → response → close."""
    with IPCClient(endpoint_path, expected_build_id=expected_build_id) as client:
        return client.request(envelope)
