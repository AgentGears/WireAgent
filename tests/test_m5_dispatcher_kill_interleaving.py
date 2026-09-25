"""Kill-switch interleaving regression for Dispatcher serialization."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.session import SessionManager


class _SB:
    pass


class _Session(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _SB()  # type: ignore[assignment]
        self._started = True


class _BlockingRead:
    name = "m5_test_kill_blocking_read"
    tier = CapabilityTier.READ

    def __init__(self, entered: asyncio.Event, release: asyncio.Event) -> None:
        self.entered = entered
        self.release = release

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        self.entered.set()
        await self.release.wait()
        return ok_result(data={"done": True})


class _ProbeRead:
    name = "m5_test_kill_probe_read"
    tier = CapabilityTier.READ

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        del broker, input
        raise AssertionError("a tripped kill switch must refuse before capability entry")


def _dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _Session(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    dispatcher._broker = object()  # type: ignore[assignment]
    return dispatcher


async def test_kill_is_acknowledged_without_waiting_for_active_invocation(
    tmp_path: Path,
) -> None:
    dispatcher = _dispatcher(tmp_path)
    entered = asyncio.Event()
    release = asyncio.Event()
    dispatcher._registry.register(_BlockingRead(entered, release))
    dispatcher._registry.register(_ProbeRead())

    blocking = asyncio.create_task(dispatcher.invoke(_BlockingRead.name))
    await entered.wait()
    dispatcher.kill_switch.trip()

    killed = await asyncio.wait_for(
        dispatcher.invoke(_ProbeRead.name),
        timeout=0.25,
    )

    assert killed.ok is False
    assert killed.failure_category.value == "security"
    assert "Kill switch tripped" in killed.error.message
    assert blocking.done() is False

    release.set()
    first = await blocking
    assert first.ok is True
