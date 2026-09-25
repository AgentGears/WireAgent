"""Dispatcher serialization regressions for staged Layer-5 migration."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_read_broker import M5LeasedReadBroker
from webwire.session import SessionManager


class _SB:
    pass


class _Session(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _SB()  # type: ignore[assignment]
        self._started = True


class _BlockingRead:
    name = "m5_test_blocking_read"
    tier = CapabilityTier.READ

    def __init__(self, entered: asyncio.Event, release: asyncio.Event, events: list[str]) -> None:
        self.entered = entered
        self.release = release
        self.events = events

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        self.events.append("blocking:start")
        self.entered.set()
        await self.release.wait()
        self.events.append("blocking:end")
        return ok_result(data={"done": True})


class _ProbeRead:
    name = "m5_test_probe_read"
    tier = CapabilityTier.READ

    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        self.events.append("probe")
        return ok_result(data={"probe": True})


class _ReentrantRead:
    name = "m5_test_reentrant_read"
    tier = CapabilityTier.READ

    def __init__(self, dispatcher: Dispatcher, events: list[str]) -> None:
        self.dispatcher = dispatcher
        self.events = events

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        self.events.append("outer:start")
        nested = await self.dispatcher.invoke(_ProbeRead.name)
        assert nested.ok is True
        self.events.append("outer:end")
        return ok_result(data={"nested": True})


class _StopInsideRead:
    name = "m5_test_stop_inside_read"
    tier = CapabilityTier.READ

    def __init__(self, dispatcher: Dispatcher) -> None:
        self.dispatcher = dispatcher

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        return await self.dispatcher.stop()


def _dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _Session(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    # Custom test capabilities do not touch this broker, but Dispatcher requires
    # the started boundary to be present before dispatching any capability.
    dispatcher._broker = object()  # type: ignore[assignment]
    return dispatcher


async def test_sibling_dispatcher_invocations_are_serialized(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []
    dispatcher._registry.register(_BlockingRead(entered, release, events))
    dispatcher._registry.register(_ProbeRead(events))

    blocking = asyncio.create_task(dispatcher.invoke(_BlockingRead.name))
    await entered.wait()
    probe = asyncio.create_task(dispatcher.invoke(_ProbeRead.name))
    await asyncio.sleep(0)

    assert events == ["blocking:start"]
    assert probe.done() is False

    release.set()
    first, second = await asyncio.gather(blocking, probe)

    assert first.ok is True
    assert second.ok is True
    assert events == ["blocking:start", "blocking:end", "probe"]


async def test_same_task_dispatcher_reentry_does_not_deadlock(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    events: list[str] = []
    dispatcher._registry.register(_ProbeRead(events))
    dispatcher._registry.register(_ReentrantRead(dispatcher, events))

    result = await dispatcher.invoke(_ReentrantRead.name)

    assert result.ok is True
    assert events == ["outer:start", "probe", "outer:end"]


async def test_sibling_stop_waits_for_active_invocation(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    events: list[str] = []
    dispatcher._registry.register(_BlockingRead(entered, release, events))

    blocking = asyncio.create_task(dispatcher.invoke(_BlockingRead.name))
    await entered.wait()
    stop_task = asyncio.create_task(dispatcher.stop())
    await asyncio.sleep(0)

    assert stop_task.done() is False
    assert dispatcher._broker is not None

    release.set()
    invocation_result, stop_result = await asyncio.gather(blocking, stop_task)

    assert invocation_result.ok is True
    assert stop_result.ok is True
    assert events == ["blocking:start", "blocking:end"]
    assert dispatcher._broker is None
    assert dispatcher._m5_stack is None


async def test_stop_from_inside_active_invocation_fails_closed(tmp_path: Path) -> None:
    dispatcher = _dispatcher(tmp_path)
    dispatcher._registry.register(_StopInsideRead(dispatcher))

    result = await dispatcher.invoke(_StopInsideRead.name)

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "inside an active invocation" in result.error.message
    assert dispatcher._broker is not None


async def test_dispatcher_start_installs_lease_aware_read_broker(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _Session(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]

    result = await dispatcher.start()

    assert result.ok is True
    assert isinstance(dispatcher._broker, M5LeasedReadBroker)
    assert dispatcher._m5_stack is not None
    assert dispatcher._broker is dispatcher._m5_stack.read_broker
    assert dispatcher._broker._m5_write_state is dispatcher._m5_stack.write_broker._m5_write_state
