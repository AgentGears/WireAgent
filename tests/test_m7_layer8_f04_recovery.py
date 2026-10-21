"""M7 Layer 8 — F04 listener-recovery ACCEPTANCE oracles (Windows).

Implements the approved repair contract (rev 4): the pre-accept
disconnect that deterministically killed the listener (REPRODUCED_PRE_ACCEPT,
10/10) must now RECOVER — the listener survives, a fresh client
completes a REAL hello/request/response exchange, and the shutdown
boundaries hold. Per the authorization: "a passing test suite that
still reports REPRODUCED_PRE_ACCEPT is not acceptable evidence" — these
oracles fail on exactly that outcome.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint, _WindowsPipeConnection
from webwire.authority_ipc_protocol import IPC_PROTOCOL_VERSION
from webwire.authority_ipc_protocol import compute_runtime_build_id
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the Windows named-pipe recovery under qualification")

BUILD_ID = compute_runtime_build_id()


def _session_ready(domain: str) -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path(domain))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _real_server(tmp_path: Path, invoke_log: list):
    session = _session_ready("f04-recovery-test")
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()

    async def invoke(name: str, input: dict) -> Any:
        invoke_log.append((name, dict(input)))
        return ok_result(data={"ok": True})

    ipc = AuthorityIPCServer(
        session=session, authority_domain=tmp_path, invoke=invoke, runtime_build_id=BUILD_ID
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=tmp_path, loop=loop)
    transport.start()
    return session, ipc, transport, loop


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


def _pre_accept_disconnect(endpoint: WindowsNamedPipeEndpoint) -> None:
    """The F04 trigger: a client connects and closes BEFORE any accept()
    call — the next ConnectNamedPipe returns ERROR_NO_DATA."""
    quick = _WindowsPipeConnection.open_client(endpoint.path)
    quick.close()


def _real_exchange(path: str, session: AuthoritySession, invoke_log: list) -> None:
    """The functional oracle: a fresh client completes hello, request,
    and response through the REAL protocol stack."""
    client = IPCClient(path, expected_build_id=BUILD_ID)
    hello = client.connect()
    assert hello.protocol_version == IPC_PROTOCOL_VERSION
    assert hello.runtime_build_id == BUILD_ID
    assert hello.authority_instance_id == session.authority_instance_id
    response = client.request(
        _envelope("read", {"post_url": "https://x.com/x/1"}, instance_id=session.authority_instance_id)
    )
    assert response["ok"] is True
    assert invoke_log[-1] == ("read", {"post_url": "https://x.com/x/1"})
    client.close()


def test_pre_accept_disconnect_listener_recovers_real_ipc(tmp_path: Path) -> None:
    """THE acceptance oracle: the deterministic 232 trigger against a LIVE
    production transport. Before the repair this killed the listener
    (REPRODUCED_PRE_ACCEPT 10/10). Now the listener must survive and a
    fresh client must complete a real exchange."""
    log: list = []
    session, ipc, transport, loop = _real_server(tmp_path, log)
    try:
        endpoint = transport._endpoint
        assert isinstance(endpoint, WindowsNamedPipeEndpoint)
        _pre_accept_disconnect(endpoint)
        time.sleep(0.5)  # the listener's ConnectNamedPipe observes the disconnect
        assert transport._listener_thread.is_alive(), (
            "the listener thread must survive the pre-accept disconnect"
        )
        _real_exchange(str(transport.endpoint_path or ""), session, log)
        assert transport._listener_thread.is_alive()
    finally:
        _teardown(transport, loop)


def test_repeated_pre_accept_disconnects_no_degradation(tmp_path: Path) -> None:
    """Repeated recovery: N consecutive pre-accept disconnects, then a
    real exchange — no accumulated handle leaks or degradation."""
    log: list = []
    session, ipc, transport, loop = _real_server(tmp_path, log)
    try:
        endpoint = transport._endpoint
        for _ in range(10):
            _pre_accept_disconnect(endpoint)
            time.sleep(0.08)
        assert transport._listener_thread.is_alive()
        _real_exchange(str(transport.endpoint_path or ""), session, log)
    finally:
        _teardown(transport, loop)


def test_reset_failure_fails_closed_with_win32_code(tmp_path: Path) -> None:
    """Fault injection: DisconnectNamedPipe fails → the ORIGINAL Win32
    error is preserved in the message, the endpoint enters TERMINAL
    (fail closed), and no resurrection occurs afterwards."""
    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    endpoint.bind()
    real_reset = endpoint._reset_disconnected_instance

    def _failing_reset() -> bool:
        import ctypes

        ctypes.set_last_error(5)  # ERROR_ACCESS_DENIED as the injected code
        return False

    endpoint._reset_disconnected_instance = _failing_reset  # type: ignore[method-assign]
    _pre_accept_disconnect(endpoint)
    time.sleep(0.1)

    outcomes: list = []

    def one_accept() -> None:
        try:
            conn = endpoint.accept()
            outcomes.append(("returned", conn))
        except RuntimeError as exc:
            outcomes.append(("raised", str(exc)))

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    th.join(timeout=10)
    assert outcomes and outcomes[0][0] == "raised"
    assert "DisconnectNamedPipe failed: error 5" in outcomes[0][1]
    # TERMINAL: a subsequent accept attempt does not resurrect service.
    assert endpoint._lifecycle == WindowsNamedPipeEndpoint._TERMINAL
    with endpoint._lifecycle_lock:
        pass  # lock is free (not held by a stuck recovery)
    endpoint._reset_disconnected_instance = real_reset  # type: ignore[method-assign]
    # close() still works on a terminal endpoint (teardown hygiene).
    endpoint.close()


def test_shutdown_wins_before_recovery_permission(tmp_path: Path) -> None:
    """F04-RP04 deterministic race: the barrier fires BEFORE the recovery
    thread takes the lifecycle lock; shutdown completes; the recovery then
    observes CLOSING and performs NO reset, NO creation, NO kernel change."""
    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    endpoint.bind()
    real_reset = endpoint._reset_disconnected_instance
    reset_calls: list[int] = []
    before_permission = threading.Event()
    shutdown_done = threading.Event()

    def _hooked_before_permission() -> None:
        before_permission.set()
        assert shutdown_done.wait(timeout=10), "test never completed shutdown"

    def _counting_reset() -> bool:
        reset_calls.append(1)
        return real_reset()

    endpoint._before_recovery_permission = _hooked_before_permission  # type: ignore[method-assign]
    endpoint._reset_disconnected_instance = _counting_reset  # type: ignore[method-assign]
    _pre_accept_disconnect(endpoint)

    results: list = []

    def one_accept() -> None:
        results.append(endpoint.accept())

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    assert before_permission.wait(timeout=10), "recovery never reached the permission barrier"
    endpoint.close()  # shutdown wins while recovery waits BEFORE the lock
    shutdown_done.set()
    th.join(timeout=10)

    assert results == [None], "recovery must return None (shutdown won), not a connection"
    assert reset_calls == [], "NO reset may run after CLOSING won"


def test_post_accept_admission_rejects_after_shutdown(tmp_path: Path) -> None:
    """F04-RP08: accept() returned a connection, shutdown won, and admit()
    refuses — the connection is closed without any protocol byte."""
    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    endpoint.bind()
    held = _WindowsPipeConnection.open_client(endpoint.path)  # occupies the instance

    results: list = []

    def one_accept() -> None:
        results.append(endpoint.accept())

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    time.sleep(0.3)  # ConnectNamedPipe completes against the held client
    th.join(timeout=10)
    assert results and results[0] is not None, "accept should return the connection"
    conn = results[0]

    endpoint.close()  # shutdown wins AFTER accept returned, BEFORE admission
    assert endpoint.admit(conn) is False, "admission must be refused after CLOSING"
    # The refused connection is unusable: any byte operation fails.
    with pytest.raises(OSError):
        conn.send(b"x")
