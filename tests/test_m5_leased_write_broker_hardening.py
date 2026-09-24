"""Adversarial hardening tests for the live-required M5 leased broker."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch


class _DeleteCDP:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def evaluate(self, expr: str) -> ActionResult:
        if "data-wireagent-delete-menu-baseline" in expr and "baselined" in expr:
            self.events.append("baseline_menu")
            value = "baselined"
        elif "caret_clicked" in expr:
            self.events.append("target_caret")
            value = "caret_clicked"
        elif "data-wireagent-delete-menu" in expr and "found.length" in expr:
            self.events.append("bind_menu")
            value = "bound"
        elif "data-wireagent-delete-confirm-baseline" in expr and "baselined" in expr:
            self.events.append("baseline_confirm")
            value = "baselined"
        elif "data-wireagent-delete-item" in expr and "item.click()" in expr:
            self.events.append("click_bound_menu_item")
            value = "clicked"
        elif "data-wireagent-delete-confirm" in expr and "found.length" in expr:
            self.events.append("bind_confirm")
            value = "bound"
        elif "data-wireagent-delete-confirm" in expr and "b.click()" in expr:
            self.events.append("click_bound_confirm")
            value = "clicked"
        elif 'return "found"' in expr:
            self.events.append("target_article")
            value = "found"
        else:
            value = None
        return ok_result(data={"result": {"value": value}})


class _Controller:
    def __init__(self, events: list[str]) -> None:
        self._cdp = _DeleteCDP(events)


class _SB:
    def __init__(self) -> None:
        self.events: list[str] = []
        self._controller = _Controller(self.events)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append(f"navigate:{url}")
        return ok_result(data={"url": url})



def _broker(tmp_path: Path) -> tuple[M5LeasedWriteBroker, _SB]:
    sb = _SB()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    broker._DELETE_POLL_INTERVAL_S = 0.0
    broker._DELETE_STAGE_TIMEOUT_S = 0.05
    return broker, sb


async def test_delete_binds_new_menu_and_confirm_before_commit(tmp_path: Path) -> None:
    broker, sb = _broker(tmp_path)

    def gate() -> None:
        sb.events.append("gate")
        return None

    result = await broker.delete_post(
        "https://x.com/u/status/123",
        "123",
        _commit_gate=gate,
    )
    assert result.ok
    assert sb.events == [
        "navigate:https://x.com/u/status/123",
        "target_article",
        "baseline_menu",
        "target_caret",
        "bind_menu",
        "baseline_confirm",
        "click_bound_menu_item",
        "bind_confirm",
        "gate",
        "click_bound_confirm",
    ]


def test_delete_binding_js_excludes_preexisting_transients() -> None:
    menu = M5LeasedWriteBroker._bind_new_delete_menu_js("old-menu", "new-menu")
    confirm = M5LeasedWriteBroker._bind_new_delete_confirm_js("old-confirm", "new-confirm")
    click = M5LeasedWriteBroker._click_bound_delete_confirm_js("new-confirm")

    assert "data-wireagent-delete-menu-baseline" in menu
    assert "continue" in menu
    assert "found.length!==1" in menu
    assert "data-wireagent-delete-confirm-baseline" in confirm
    assert "found.length!==1" in confirm
    assert "data-wireagent-delete-confirm" in click
    assert "querySelector('[data-wireagent-delete-confirm=" in click


async def test_failed_content_start_releases_lease_when_no_context_bound(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)

    async def fail_before_context() -> ActionResult:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await broker._start_content(fail_before_context)
    assert broker._m5_write_state.content_owner is None


async def test_failed_content_start_retains_lease_after_context_binding(tmp_path: Path) -> None:
    broker, _ = _broker(tmp_path)

    async def fail_after_context() -> ActionResult:
        broker._m5_context_token = "ctx"
        broker._m5_context_kind = "post"
        broker._m5_context_target = "none"
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await broker._start_content(fail_after_context)
    assert broker._m5_write_state.content_owner == broker._m5_lease_owner
