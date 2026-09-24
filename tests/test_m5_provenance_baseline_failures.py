"""Fail-closed regressions for Layer-4 DOM provenance baselines."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_scoped_write_broker import M5ScopedWriteBroker
from webwire.safety.kill_switch import KillSwitch

URL = "https://x.com/u/status/123"
TARGET = "123"


class _CDP:
    def __init__(self, events: list[str], fail: str) -> None:
        self.events = events
        self.fail = fail

    def _baseline(self, name: str) -> ActionResult:
        self.events.append(name)
        if self.fail == name:
            return soft_failure(f"forced {name} failure")
        return ok_result(data={"result": {"value": "baselined"}})

    async def evaluate(self, expr: str) -> ActionResult:
        if "data-wireagent-context-baseline" in expr and "baselined" in expr:
            return self._baseline("composer_baseline")
        if "data-wireagent-menu-baseline" in expr and "baselined" in expr:
            return self._baseline("quote_menu_baseline")
        if "data-wireagent-media-input" in expr and "return 'bound'" in expr:
            self.events.append("bind_media_input")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-media-baseline" in expr:
            return self._baseline("media_baseline")
        if "m5-context-reply" in expr:
            self.events.append("click_reply")
            return ok_result(data={"result": {"value": "clicked"}})
        if "m5-context-quote" in expr:
            self.events.append("click_repost")
            return ok_result(data={"result": {"value": "clicked"}})
        if "data-wireagent-delete-menu-baseline" in expr and "baselined" in expr:
            return self._baseline("delete_menu_baseline")
        if "data-wireagent-delete-confirm-baseline" in expr and "baselined" in expr:
            return self._baseline("delete_confirm_baseline")
        if 'return "found"' in expr:
            self.events.append("target_article")
            return ok_result(data={"result": {"value": "found"}})
        if "caret_clicked" in expr:
            self.events.append("click_caret")
            return ok_result(data={"result": {"value": "caret_clicked"}})
        if "data-wireagent-delete-menu" in expr and "found.length" in expr:
            self.events.append("bind_delete_menu")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-delete-item" in expr and "item.click()" in expr:
            self.events.append("click_delete_item")
            return ok_result(data={"result": {"value": "clicked"}})
        if "data-wireagent-delete-confirm" in expr and "found.length" in expr:
            self.events.append("bind_delete_confirm")
            return ok_result(data={"result": {"value": "bound"}})
        if "confirmationSheetCancel" in expr:
            self.events.append("dismiss_delete")
            return ok_result(data={"result": {"value": "cancelled"}})
        return ok_result(data={"result": {"value": None}})


class _Controller:
    def __init__(self, events: list[str], fail: str) -> None:
        self._cdp = _CDP(events, fail)


class _SB:
    def __init__(self, fail: str) -> None:
        self.events: list[str] = []
        self._controller = _Controller(self.events, fail)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append(f"navigate:{url}")
        return ok_result(data={"url": url})

    async def upload_file(self, selector: str, path: str) -> ActionResult:
        self.events.append(f"upload:{Path(path).name}")
        return ok_result(data={"uploaded": True})


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _scoped(tmp_path: Path, fail: str) -> tuple[M5ScopedWriteBroker, _SB]:
    sb = _SB(fail)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return M5ScopedWriteBroker(sb, KillSwitch(cfg)), sb  # type: ignore[arg-type]


def _leased(tmp_path: Path, fail: str) -> tuple[M5LeasedWriteBroker, _SB]:
    sb = _SB(fail)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    broker._DELETE_POLL_INTERVAL_S = 0.0
    broker._DELETE_STAGE_TIMEOUT_S = 0.05
    return broker, sb


async def test_reply_composer_baseline_failure_prevents_target_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _scoped(tmp_path, "composer_baseline")

    result = await broker.open_reply_on_target(URL, TARGET)

    assert not result.ok
    assert "composer_baseline" in sb.events
    assert "click_reply" not in sb.events


async def test_quote_menu_baseline_failure_prevents_repost_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _scoped(tmp_path, "quote_menu_baseline")

    result = await broker.open_quote_on_target(URL, TARGET)

    assert not result.ok
    assert sb.events[:3] == [
        f"navigate:{URL}",
        "composer_baseline",
        "quote_menu_baseline",
    ]
    assert "click_repost" not in sb.events


async def test_media_baseline_failure_prevents_upload(tmp_path: Path) -> None:
    broker, sb = _scoped(tmp_path, "media_baseline")
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = "post"
    broker._m5_context_target = "none"

    result = await broker.attach_media(str(tmp_path / "approved.png"))

    assert not result.ok
    assert sb.events == ["bind_media_input", "media_baseline"]
    assert not any(event.startswith("upload:") for event in sb.events)


async def test_delete_menu_baseline_failure_prevents_caret_and_commit(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _leased(tmp_path, "delete_menu_baseline")
    gate_called = False

    def gate() -> None:
        nonlocal gate_called
        gate_called = True
        return None

    result = await broker.delete_post(URL, TARGET, _commit_gate=gate)

    assert not result.ok
    assert "target_article" in sb.events
    assert "delete_menu_baseline" in sb.events
    assert "click_caret" not in sb.events
    assert not gate_called


async def test_delete_confirm_baseline_failure_prevents_delete_item_and_commit(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _leased(tmp_path, "delete_confirm_baseline")
    gate_called = False

    def gate() -> None:
        nonlocal gate_called
        gate_called = True
        return None

    result = await broker.delete_post(URL, TARGET, _commit_gate=gate)

    assert not result.ok
    assert "click_caret" in sb.events
    assert "bind_delete_menu" in sb.events
    assert "delete_confirm_baseline" in sb.events
    assert "click_delete_item" not in sb.events
    assert not gate_called
