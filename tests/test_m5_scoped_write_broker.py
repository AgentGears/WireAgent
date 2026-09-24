"""Layer-4 provenance tests for M5ScopedWriteBroker."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.m5_scoped_write_broker import M5ScopedWriteBroker
from webwire.safety.kill_switch import KillSwitch

URL = "https://x.com/u/status/123"
TARGET = "123"


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
        self.proof_bind_result = "bound"
        self.proof_click_result = "clicked"

    async def evaluate(self, expr: str) -> ActionResult:
        if "data-wireagent-context-baseline" in expr and "return 'baselined'" in expr:
            self.events.append("baseline_composers")
            return ok_result(data={"result": {"value": "baselined"}})
        if "m5-context-reply" in expr:
            assert "querySelectorAll('article')" in expr
            assert "a.closest('article')!==arts[ai]" in expr
            assert "querySelector('time')" in expr
            self.events.append("click_target_reply")
            return ok_result(data={"result": {"value": "clicked"}})
        if "m5-context-quote" in expr:
            assert "querySelectorAll('article')" in expr
            assert "a.closest('article')!==arts[ai]" in expr
            self.events.append("click_target_repost")
            return ok_result(data={"result": {"value": "clicked"}})
        if "data-wireagent-menu-baseline" in expr and "return 'baselined'" in expr:
            self.events.append("baseline_menus")
            return ok_result(data={"result": {"value": "baselined"}})
        if "data-wireagent-quote-menu" in expr and "found.length" in expr:
            assert "data-wireagent-menu-baseline" in expr
            self.events.append("bind_new_quote_menu")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-quote-item" in expr and "item.click()" in expr:
            self.events.append("click_bound_quote")
            return ok_result(data={"result": {"value": "clicked"}})
        if "data-wireagent-context" in expr and "found.length" in expr:
            self.events.append("bind_new_context")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-context" in expr and "return 'focused'" in expr:
            self.events.append("focus_bound_textarea")
            return ok_result(data={"result": {"value": "focused"}})
        if "data-wireagent-media-input" in expr and "return 'bound'" in expr:
            self.events.append("bind_media_input")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-media-baseline" in expr:
            self.events.append("baseline_media")
            return ok_result(data={"result": {"value": "baselined"}})
        if "data-wireagent-approved-media" in expr and "found.length" in expr:
            assert "[data-testid='attachments']" in expr
            self.events.append("bind_new_media")
            return ok_result(data={"result": {"value": "bound"}})
        if "data-wireagent-submit-button" in expr and "candidates.length" in expr:
            assert "data-wireagent-context-kind" in expr
            assert "data-wireagent-context-target" in expr
            assert "data-wireagent-approved-media" in expr
            if "submit_changed" in expr:
                self.events.append("submit_proof_click")
                return ok_result(data={"result": {"value": self.proof_click_result}})
            self.events.append("submit_proof_bind")
            return ok_result(data={"result": {"value": self.proof_bind_result}})
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

    async def upload_file(self, selector: str, path: str) -> ActionResult:
        assert selector.startswith("[data-wireagent-media-input='")
        self.events.append(f"upload:{Path(path).name}")
        return ok_result(data={"uploaded": True})


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _broker(tmp_path: Path) -> tuple[M5ScopedWriteBroker, _SB]:
    sb = _SB()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return M5ScopedWriteBroker(sb, KillSwitch(cfg)), sb  # type: ignore[arg-type]


async def test_reply_navigation_precedes_baseline_and_context_is_target_owned(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _broker(tmp_path)
    result = await broker.open_reply_on_target(URL, TARGET)
    assert result.ok
    assert sb.events[:4] == [
        f"navigate:{URL}",
        "baseline_composers",
        "click_target_reply",
        "bind_new_context",
    ]
    assert (await broker.fill_reply_composer("approved")).ok
    assert sb.events[-2:] == ["focus_bound_textarea", "type:approved"]


async def test_quote_baselines_destination_menus_before_target_repost(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _broker(tmp_path)
    result = await broker.open_quote_on_target(URL, TARGET)
    assert result.ok
    assert sb.events[:7] == [
        f"navigate:{URL}",
        "baseline_composers",
        "baseline_menus",
        "click_target_repost",
        "bind_new_quote_menu",
        "click_bound_quote",
        "bind_new_context",
    ]


async def test_approved_upload_is_bound_to_exact_new_preview(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("webwire.m5_scoped_write_broker.asyncio.sleep", _no_sleep)
    broker, sb = _broker(tmp_path)
    broker._m5_context_token = "ctx"  # test the broker provenance seam directly
    broker._m5_context_kind = "post"
    broker._m5_context_target = "none"
    result = await broker.attach_media(str(tmp_path / "approved.png"))
    assert result.ok
    assert result.data and result.data.get("provenance_bound") is True
    assert sb.events == [
        "bind_media_input",
        "baseline_media",
        "upload:approved.png",
        "bind_new_media",
    ]


async def test_same_count_unapproved_media_fails_before_commit_gate(tmp_path: Path) -> None:
    broker, sb = _broker(tmp_path)
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = "post"
    broker._m5_context_target = "none"
    sb._controller._cdp.proof_bind_result = "media_unapproved"
    gates: list[str] = []

    async def check() -> Optional[ActionResult]:
        return None

    result = await broker.click_submit(
        _precommit_check=check,
        _commit_gate=lambda: gates.append("gate") or None,
        _expected_text="approved",
        _expected_attachments=1,
    )
    assert not result.ok
    assert gates == []
    assert sb.events == ["submit_proof_bind"]


async def test_submit_replacement_after_gate_is_unknown_and_not_clicked(tmp_path: Path) -> None:
    broker, sb = _broker(tmp_path)
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = "reply"
    broker._m5_context_target = TARGET
    sb._controller._cdp.proof_click_result = "submit_changed"
    gates: list[str] = []

    async def check() -> Optional[ActionResult]:
        return None

    result = await broker.click_submit(
        _precommit_check=check,
        _commit_gate=lambda: gates.append("gate") or None,
        _expected_text="approved",
        _expected_attachments=0,
    )
    assert not result.ok
    assert gates == ["gate"]
    assert sb.events == ["submit_proof_bind", "submit_proof_click"]
