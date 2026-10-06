"""M7 Layer 4 — the operational IPC transport (F-60, F-66, F-67).

A real accept loop over the QUALIFIED platform endpoint (F-66: the
transport does NOT bind its own socket — it consumes the
``IPCEndpoint`` abstraction that owns umask-before-bind, stale-path
cleanup under ownership, and unlink-on-close). One request per
connection. Connection-handler slots are bounded BEFORE task creation.

The client (F-67) uses one bounded receive primitive: read the 8-byte
header → reject lengths over the applicable ceiling → read exactly that
many body bytes → decode with the strict JSON decoder. No unbounded
allocation happens before the owner's identity is verified.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
from pathlib import Path
from typing import Any, Optional

from webwire.authority_ipc_endpoint import IPCEndpoint, create_endpoint
from webwire.authority_ipc_framing import (
    HEADER_SIZE,
    FrameHeader,
    decode_json_frame,
    encode_json_frame,
)
from webwire.authority_ipc_protocol import (
    IPC_MAX_FRAME_BYTES,
    IPC_MAX_REQUEST_BYTES,
    IPC_MAX_RESPONSE_BYTES,
    IPC_PROTOCOL_VERSION,
    AuthorityHello,
)
from webwire.authority_ipc_server import AuthorityIPCServer

logger = logging.getLogger(__name__)

__all__ = ["IPCTransportServer", "IPCClient", "connect_and_request"]


def _read_exact(sock: Any, n: int) -> bytes:
    """Read exactly n bytes from a socket/pipe or raise ConnectionError."""
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(min(n - len(buf), 65536))
        if not chunk:
            raise ConnectionError(f"connection closed after {len(buf)}/{n} bytes")
        buf += chunk
    return buf


def _write_all(sock: Any, data: bytes) -> None:
    """Write all bytes to a socket/pipe or raise ConnectionError."""
    sent = 0
    while sent < len(data):
        n = sock.send(data[sent:])
        if n == 0:
            raise ConnectionError("connection closed during write")
        sent += n


def _bounded_receive(sock: Any, *, ceiling: int) -> tuple[FrameHeader, dict[str, Any]]:
    """F-67: one bounded receive — read header, reject lengths over the
    ceiling BEFORE any body allocation, read exactly the announced body,
    then decode with the strict JSON decoder."""
    header_bytes = _read_exact(sock, HEADER_SIZE)
    (length,) = struct.unpack(">Q", header_bytes)
    if length > ceiling:
        raise ValueError(
            f"announced length {length} exceeds the {ceiling}-byte ceiling; refused before body allocation"
        )
    body = _read_exact(sock, length)
    header = FrameHeader(length=length)
    return header, decode_json_frame(header, body)


class IPCTransportServer:
    """The operational transport: a bounded accept loop over the QUALIFIED
    endpoint (F-66: reuses IPCEndpoint — never binds its own socket).

    ``start()`` creates+binds the qualified endpoint and begins the
    bounded listener. ``stop()`` closes tracked connections, then the
    endpoint (which unlinks the rendezvous). Between them, real clients
    can connect, exchange hello, send one framed request, and receive
    one framed response."""

    def __init__(
        self,
        *,
        ipc_server: AuthorityIPCServer,
        authority_domain: Path,
        loop: asyncio.AbstractEventLoop,
        max_concurrent: int = 4,
    ) -> None:
        self._ipc = ipc_server
        self._authority_domain = authority_domain
        self._loop = loop
        self._max_concurrent = max_concurrent
        self._handler_slots = threading.Semaphore(max_concurrent)  # F-62: from param
        self._endpoint: Optional[IPCEndpoint] = None
        self._running = False
        self._connections: set[Any] = set()  # F-60: tracked for shutdown
        self._connections_lock = threading.Lock()

    @property
    def endpoint_path(self) -> Optional[str]:
        return self._endpoint.path if self._endpoint else None

    def start(self) -> None:
        """Create+bind the QUALIFIED endpoint (umask, stale cleanup,
        0600) and start the bounded accept loop."""
        self._endpoint = create_endpoint(self._authority_domain)
        self._endpoint.bind()  # F-66: the qualified endpoint owns all security
        self._running = True
        thread = threading.Thread(target=self._accept_loop, daemon=True, name="webwire-ipc-listener")
        thread.start()
        self._listener_thread = thread

    _listener_thread: Optional[threading.Thread] = None

    def stop(self) -> None:
        """Close tracked connections, then the endpoint (which unlinks)."""
        self._running = False
        # Close the endpoint first (stops accepting).
        if self._endpoint is not None:
            try:
                self._endpoint.close()  # F-66: unlinks the rendezvous
            finally:
                self._endpoint = None
        # Close all tracked live connections (idle handlers exit).
        with self._connections_lock:
            for conn in list(self._connections):
                try:
                    conn.close()
                except OSError:
                    pass
            self._connections.clear()
        if self._listener_thread is not None:
            self._listener_thread.join(timeout=5)
            self._listener_thread = None

    def _accept_loop(self) -> None:
        """Accept connections via the QUALIFIED endpoint; bounded handlers."""
        while self._running and self._endpoint is not None:
            try:
                conn = self._endpoint.accept()
            except (OSError, RuntimeError):
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
            with self._connections_lock:
                self._connections.add(conn)
            thread = threading.Thread(
                target=self._handle_connection,
                args=(conn,),
                daemon=True,
                name="webwire-ipc-handler",
            )
            thread.start()

    def _handle_connection(self, conn: Any) -> None:
        """One connection: hello → one framed request → response → close."""
        try:
            hello = self._ipc._hello()
            hello_frame = encode_json_frame(hello.to_dict(), is_response=True)
            _write_all(conn, hello_frame)

            header_bytes = _read_exact(conn, HEADER_SIZE)
            (length,) = struct.unpack(">Q", header_bytes)
            # F-62: enforce BOTH ceilings from the header at the transport.
            if length > IPC_MAX_FRAME_BYTES or length > IPC_MAX_REQUEST_BYTES:
                return  # close without reading the body
            body = _read_exact(conn, length)

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
            try:
                _write_all(conn, response_frame)
            except (ConnectionError, OSError):
                # F-60: the conservative disconnect law — the response is
                # lost but the owner work completed inside process_request.
                pass
        except (ConnectionError, OSError, ValueError):
            pass
        finally:
            with self._connections_lock:
                self._connections.discard(conn)
            self._handler_slots.release()
            try:
                conn.close()
            except OSError:
                pass


class IPCClient:
    """A minimal IPC client with BOUNDED receive framing (F-67).

    ``connect()`` reads and validates AuthorityHello using the same
    bounded receive + strict decoder as the server — no unbounded
    allocation before the owner's identity is verified. ``request()``
    sends one framed request and reads one framed response with the
    same bounded receive."""

    def __init__(self, endpoint_path: str, *, expected_build_id: str) -> None:
        self._path = endpoint_path
        self._expected_build = expected_build_id
        self._sock: Optional[Any] = None
        self._hello: Optional[AuthorityHello] = None

    def connect(self) -> AuthorityHello:
        """Connect, read hello with BOUNDED framing, verify exact
        protocol + build."""
        if not hasattr(socket, "AF_UNIX"):
            raise RuntimeError("AF_UNIX sockets are POSIX-only")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self._path)
        self._sock = sock
        # F-67: bounded receive with the strict decoder — the response
        # ceiling bounds the hello (a hello IS a response-shaped frame).
        try:
            _, hello_raw = _bounded_receive(sock, ceiling=IPC_MAX_RESPONSE_BYTES)
        except (ValueError, ConnectionError):
            self.close()
            raise
        hello = AuthorityHello.from_dict(hello_raw)
        if hello.protocol_version != IPC_PROTOCOL_VERSION:
            self.close()
            raise ValueError(
                f"protocol mismatch: server={hello.protocol_version}, client={IPC_PROTOCOL_VERSION}"
            )
        if hello.runtime_build_id != self._expected_build:
            self.close()
            raise ValueError(
                f"build mismatch: server={hello.runtime_build_id[:16]}…, client={self._expected_build[:16]}…"
            )
        self._hello = hello
        return hello

    def request(self, envelope: dict[str, Any]) -> dict[str, Any]:
        """Send one framed request, read one framed response (bounded)."""
        if self._sock is None:
            raise RuntimeError("not connected")
        frame = encode_json_frame(envelope)
        _write_all(self._sock, frame)
        # F-67: bounded receive for the response.
        _, response = _bounded_receive(self._sock, ceiling=IPC_MAX_RESPONSE_BYTES)
        return response

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
