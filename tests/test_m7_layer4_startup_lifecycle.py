"""M7 Layer 4 startup lifecycle tests (F-69): the READY-gated listener.

The frozen startup order is:

    bind endpoint → prepare PARKED listener thread → READY → release the
    accept gate (non-failing Event.set)

A listener-thread creation failure must therefore land while the session
is still STARTING — where the normal fail-closed startup cleanup applies
(session revoke → browser quiesce → endpoint close/unlink → TERMINAL →
ownership release). It must never strand a READY owner with a bound
endpoint and no accept loop.
"""

from __future__ import annotations

import os
import socket
from pathlib import Path
from typing import Any

from webwire.authority import AuthorityOwnerLock
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _RecordingSessionManager(SessionManager):
    """Session double that records lifecycle calls; start always succeeds."""

    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = False
        self._start_calls = 0
        self._stop_calls = 0

    async def start(self) -> Any:
        self._start_calls += 1
        self._started = True
        return ok_result(data={})

    async def stop(self) -> Any:
        self._stop_calls += 1
        self._started = False
        return ok_result(data={})


def _ipc_dispatcher(tmp_path: Path):
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _RecordingSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm, enable_ipc=True)  # type: ignore[arg-type]
    return d, sm, cfg


def _stub_the_m5_stack(monkeypatch, d: Dispatcher) -> None:
    """Stub the live M5 install (orthogonal to the lifecycle under test)."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        d,
        "_install_m5_live_stack",
        lambda sb: setattr(d, "_m5_stack", SimpleNamespace(read_broker=object())),
    )


class _ExplodingThread:
    """Thread stand-in whose start() fails — the F-69 injected failure."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    def start(self) -> None:
        raise RuntimeError("cannot create another thread (F-69 injection)")


async def test_listener_thread_failure_never_ready(tmp_path: Path, monkeypatch) -> None:
    """Thread.start() failure inside the guarded STARTING window: start()
    refuses with a security hard_failure and the full startup cleanup
    runs — session revoked+TERMINAL (never READY), browser/root quiesced,
    endpoint gone, ownership released for a successor."""
    d, sm, cfg = _ipc_dispatcher(tmp_path)
    _stub_the_m5_stack(monkeypatch, d)

    import threading

    monkeypatch.setattr(threading, "Thread", _ExplodingThread)
    try:
        result = await d.start()
    finally:
        monkeypatch.undo()  # restore real threads for the teardown assertions

    assert result.ok is False
    message = getattr(result.error, "message", "")
    assert "IPC startup failed" in message, message
    assert "cannot create another thread" in message, message

    # Never READY: the STARTING cleanup path ran to completion — revoke
    # succeeded (impossible from READY per the F-59 state gate), the
    # session is TERMINAL, and no session was ever activated.
    session = d._authority_session
    assert session is not None
    assert session.state.value == "terminal"

    # Browser/root quiesced before ownership release.
    assert sm._stop_calls == 1
    assert sm._started is False

    # No accepting endpoint outlives the owner: the transport was closed
    # and cleared.
    assert d._ipc_transport is None
    if hasattr(socket, "AF_UNIX"):
        assert not os.path.exists(tmp_path / "authority.sock")  # noqa: ASYNC240

    # Ownership released: a successor acquires the same domain freely.
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


async def test_ready_owner_has_live_acceptor(tmp_path: Path, monkeypatch) -> None:
    """The positive path: with IPC enabled, a successful start() reaches
    READY with the accept gate RELEASED and a real client receives the
    hello — a READY owner always has a live accept loop."""
    d, sm, cfg = _ipc_dispatcher(tmp_path)
    _stub_the_m5_stack(monkeypatch, d)

    result = await d.start()
    try:
        assert result.ok is True, getattr(result.error, "message", result)
        transport = d._ipc_transport
        assert transport is not None
        assert transport._accept_gate.is_set(), "the gate must open after READY"
        assert transport.endpoint_path, "the endpoint must be bound"

        # A real client connects and receives the hello (acceptor live).
        from webwire.authority_ipc_protocol import compute_runtime_build_id
        from webwire.authority_ipc_transport import IPCClient

        client = IPCClient(transport.endpoint_path, expected_build_id=compute_runtime_build_id())
        hello = client.connect()
        assert hello.lifecycle_state == "ready"
        client.close()
    finally:
        await d.stop()


async def test_production_factory_enables_ipc(tmp_path: Path) -> None:
    """F-61: build_production_runtime constructs its dispatcher with
    enable_ipc=True — production IPC is ON through the real factory, while
    the Dispatcher default stays False (tests/offline paths stay
    endpoint-less unless they ask)."""
    from webwire.m8_card_cli import build_production_runtime
    from webwire.session import SessionManager

    seen: dict[str, Any] = {}

    class _CapturingDispatcher:
        def __init__(self, config: Any, *, session_manager: Any, enable_ipc: bool = False) -> None:
            seen["enable_ipc"] = enable_ipc
            self._session = session_manager

        async def start(self) -> Any:
            return ok_result(data={})

        async def invoke(self, capability: str, payload: Any) -> Any:
            assert capability == "whoami"
            self._session._resolved_handle = "@owner"
            return ok_result(data={"handle": "@owner"})

        async def stop(self) -> Any:
            return ok_result(data={})

    runtime = await build_production_runtime(
        WebWireConfig(state_dir=tmp_path, kill_env_var=None),
        dispatcher_factory=lambda c, *, session_manager, enable_ipc: _CapturingDispatcher(
            c, session_manager=session_manager, enable_ipc=enable_ipc
        ),
        session_factory=lambda cfg: SessionManager(cfg),
    )
    try:
        assert seen["enable_ipc"] is True, "the production factory must enable IPC"
        assert runtime is not None
    finally:
        await runtime.stop()
