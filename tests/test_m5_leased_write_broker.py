"""Regressions for the Layer-4 per-browser M5 write lease."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.kill_switch import KillSwitch


class _Keyboard:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def type(self, text: str, delay: int = 0) -> None:
        self.events.append(f"type:{text}")


class _BackendPage:
    def __init__(self, events: list[str]) -> None:
        self.keyboard = _Keyboard(events)


class _EnginePage:
    def __init__(self, events: list[str]) -> None:
        self.backend_page = _BackendPage(events)


class _Page:
    def __init__(self, events: list[str]) -> None:
        self.engine_page = _EnginePage(events)


class _CDP:
    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.text_verification = "match"
        self.plain_bind_result = "bound"

    async def evaluate(self, expr: str) -> ActionResult:
        if "data-wireagent-context-target','none'" in expr and "nonempty" in expr:
            self.events.append("bind_plain_context")
            return ok_result(data={"result": {"value": self.plain_bind_result}})
        if "data-wireagent-context-baseline" in expr and "baselined" in expr:
            self.events.append("baseline")
            return ok_result(data={"result": {"value": "baselined"}})
        if "data-wireagent-context" in expr and "found.length" in expr:
            self.events.append("bind_context")
            return ok_result(data={"result": {"value": "bound"}})
        if "return (ta.innerText||'')===expected?'match':'mismatch'" in expr:
            self.events.append("verify_bound_text")
            return ok_result(data={"result": {"value": self.text_verification}})
        if "return 'focused'" in expr and "data-wireagent-context" in expr:
            self.events.append("focus")
            return ok_result(data={"result": {"value": "focused"}})
        if "aria.indexOf(\"Close\")" in expr:
            self.events.append("close")
            return ok_result(data={"result": {"value": "closed"}})
        return ok_result(data={"result": {"value": None}})


class _Controller:
    def __init__(self, events: list[str]) -> None:
        self._cdp = _CDP(events)


class _SB:
    def __init__(self) -> None:
        self.events: list[str] = []
        self._controller = _Controller(self.events)
        self._page = _Page(self.events)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append(f"navigate:{url}")
        return ok_result(data={"url": url})

    async def click(self, selector: str, description: str = "") -> ActionResult:
        self.events.append(f"click:{selector}")
        return ok_result(data={"clicked": True})

    async def upload_file(self, selector: str, path: str) -> ActionResult:
        self.events.append(f"upload:{selector}:{Path(path).name}")
        return ok_result(data={"uploaded": True})


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _brokers(tmp_path: Path) -> tuple[M5LeasedWriteBroker, M5LeasedWriteBroker, _SB]:
    sb = _SB()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    first = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    second = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    return first, second, sb


async def test_plain_post_binds_empty_context_before_typing(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    first, _, sb = _brokers(tmp_path)

    result = await first.fill_composer("approved")
    assert result.ok
    assert sb.events[:5] == [
        "navigate:https://x.com/compose/post",
        "bind_plain_context",
        "focus",
        "type:approved",
        "verify_bound_text",
    ]


async def test_plain_post_rejects_nonempty_or_ambiguous_context_before_typing(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    first, _, sb = _brokers(tmp_path)

    sb._controller._cdp.plain_bind_result = "nonempty"
    nonempty = await first.fill_composer("approved")
    assert not nonempty.ok
    assert not any(event.startswith("type:") for event in sb.events)
    assert first._m5_write_state.content_owner is None

    sb.events.clear()
    sb._controller._cdp.plain_bind_result = "ambiguous"
    ambiguous = await first.fill_composer("approved")
    assert not ambiguous.ok
    assert not any(event.startswith("type:") for event in sb.events)
    assert first._m5_write_state.content_owner is None


async def test_bound_staging_failure_retains_lease_until_cleanup(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    first, second, sb = _brokers(tmp_path)
    sb._controller._cdp.text_verification = "mismatch"

    failed = await first.fill_composer("approved")
    assert not failed.ok
    assert first._m5_context_token is not None
    assert first._m5_write_state.content_owner == first._m5_lease_owner

    blocked = await second.fill_composer("other")
    assert not blocked.ok
    assert second._m5_context_token is None

    cleanup = await first.close_composer()
    assert cleanup.ok
    assert first._m5_write_state.content_owner is None


async def test_active_content_owner_blocks_other_m5_writer(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_leased_write_broker.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    monkeypatch.setattr("webwire.m5_write_broker.asyncio.sleep", _no_sleep)
    first, second, sb = _brokers(tmp_path)

    opened = await first.fill_composer("approved")
    assert opened.ok
    assert first._m5_write_state is second._m5_write_state
    assert first._m5_write_state.content_owner == first._m5_lease_owner

    blocked_content = await second.fill_composer("other")
    assert not blocked_content.ok
    blocked_effect = await second.click_bookmark(
        "https://x.com/u/status/123", _commit_gate=lambda: None
    )
    assert not blocked_effect.ok
    assert all("status/123" not in event for event in sb.events if event.startswith("navigate:"))

    cleanup = await first.close_composer()
    assert cleanup.ok
    assert first._m5_write_state.content_owner is None


async def test_reply_typing_must_verify_marked_context(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    first, _, sb = _brokers(tmp_path)
    first._m5_context_token = "ctx"
    first._m5_context_kind = "reply"
    first._m5_context_target = "123"
    first._m5_write_state.content_owner = first._m5_lease_owner
    sb._controller._cdp.text_verification = "mismatch"

    result = await first.fill_reply_composer("approved")
    assert not result.ok
    assert sb.events[-3:] == ["focus", "type:approved", "verify_bound_text"]


def test_media_input_binding_never_falls_back_to_page_global_input() -> None:
    expr = M5LeasedWriteBroker._mark_media_input_js("ctx", "input")
    assert "root.querySelectorAll" in expr
    assert "form.querySelectorAll" in expr
    assert "document.querySelectorAll(\"input[type='file']\")" not in expr
