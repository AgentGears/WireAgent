"""Regressions for the Layer-5 read surface sharing the M5 browser lease."""

from __future__ import annotations

import asyncio
from pathlib import Path

from webwire.broker import ReadOnlyBroker
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_read_broker import M5LeasedReadBroker
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch


class _SB:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def navigate(
        self,
        url: str,
        wait_until: str = "domcontentloaded",
    ) -> ActionResult:
        self.events.append(f"navigate:{url}:{wait_until}")
        return ok_result(data={"url": url})


def _brokers(
    tmp_path: Path,
) -> tuple[M5LeasedReadBroker, M5LeasedWriteBroker, _SB]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    sb = _SB()
    read = M5LeasedReadBroker(sb, kill, cfg)  # type: ignore[arg-type]
    write = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    return read, write, sb


def test_every_read_allowlist_method_is_explicitly_lease_coordinated() -> None:
    """A future ReadOnlyBroker method cannot silently bypass the M5 lease."""
    overridden = frozenset(M5LeasedReadBroker.__dict__)
    missing = ReadOnlyBroker._ALLOWED - overridden
    assert missing == frozenset()


def test_read_and_write_brokers_share_exact_browser_lease_state(tmp_path: Path) -> None:
    read, write, _ = _brokers(tmp_path)
    assert read._m5_write_state is write._m5_write_state


async def test_read_navigation_fails_closed_while_composer_is_owned(
    tmp_path: Path,
) -> None:
    read, write, sb = _brokers(tmp_path)
    write._m5_write_state.content_owner = write._m5_lease_owner

    result = await read.navigate("https://x.com/home")

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "active M5 content composer" in result.error.message
    assert sb.events == []


async def test_read_operation_serializes_with_m5_write_operation_lock(
    tmp_path: Path,
) -> None:
    read, write, sb = _brokers(tmp_path)
    state = write._m5_write_state
    await state.lock.acquire()
    try:
        task = asyncio.create_task(read.navigate("https://x.com/home"))
        await asyncio.sleep(0)
        assert task.done() is False
        assert sb.events == []
    finally:
        state.lock.release()

    result = await task
    assert result.ok is True
    assert sb.events == ["navigate:https://x.com/home:domcontentloaded"]
