"""Regressions for exact-head Layer-4 review findings."""

from __future__ import annotations

from pathlib import Path
from types import MethodType

import pytest
from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_scoped_write_broker import M5ScopedWriteBroker
from webwire.safety.kill_switch import KillSwitch


class _DeleteCDP:
    def __init__(
        self,
        events: list[str],
        *,
        stale_item: bool = False,
        stale_confirm: bool = False,
    ) -> None:
        self.events = events
        self.stale_item = stale_item
        self.stale_confirm = stale_confirm

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
            value = "stale" if self.stale_item else "clicked"
        elif "data-wireagent-delete-confirm" in expr and "found.length" in expr:
            self.events.append("bind_confirm")
            value = "bound"
        elif "data-wireagent-delete-confirm" in expr and "b.click()" in expr:
            self.events.append("click_bound_confirm")
            value = "stale" if self.stale_confirm else "clicked"
        elif 'return "found"' in expr:
            self.events.append("target_article")
            value = "found"
        elif "confirmationSheetCancel" in expr:
            self.events.append("dismiss_delete")
            value = "no_cancel_found"
        else:
            value = None
        return ok_result(data={"result": {"value": value}})


class _DeleteController:
    def __init__(
        self,
        events: list[str],
        *,
        stale_item: bool = False,
        stale_confirm: bool = False,
    ) -> None:
        self._cdp = _DeleteCDP(
            events,
            stale_item=stale_item,
            stale_confirm=stale_confirm,
        )


class _DeleteSB:
    def __init__(
        self,
        *,
        stale_item: bool = False,
        stale_confirm: bool = False,
    ) -> None:
        self.events: list[str] = []
        self._controller = _DeleteController(
            self.events,
            stale_item=stale_item,
            stale_confirm=stale_confirm,
        )

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append(f"navigate:{url}")
        return ok_result(data={"url": url})


def _delete_broker(
    tmp_path: Path,
    *,
    stale_item: bool = False,
    stale_confirm: bool = False,
) -> tuple[M5LeasedWriteBroker, _DeleteSB]:
    sb = _DeleteSB(stale_item=stale_item, stale_confirm=stale_confirm)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    broker = M5LeasedWriteBroker(sb, KillSwitch(cfg))  # type: ignore[arg-type]
    broker._DELETE_POLL_INTERVAL_S = 0.0
    broker._DELETE_STAGE_TIMEOUT_S = 0.05
    return broker, sb


async def test_stale_delete_menu_item_fails_before_commit(tmp_path: Path) -> None:
    broker, sb = _delete_broker(tmp_path, stale_item=True)
    gate_calls = 0

    def gate() -> None:
        nonlocal gate_calls
        gate_calls += 1
        return None

    result = await broker.delete_post(
        "https://x.com/u/status/123",
        "123",
        _commit_gate=gate,
    )

    assert not result.ok
    assert result.failure_category is FailureCategory.UNKNOWN
    assert gate_calls == 0
    assert "bind_confirm" not in sb.events


async def test_stale_delete_confirm_is_unknown_after_commit(tmp_path: Path) -> None:
    broker, sb = _delete_broker(tmp_path, stale_confirm=True)
    gate_calls = 0

    def gate() -> None:
        nonlocal gate_calls
        gate_calls += 1
        sb.events.append("gate")
        return None

    result = await broker.delete_post(
        "https://x.com/u/status/123",
        "123",
        _commit_gate=gate,
    )

    assert not result.ok
    assert result.failure_category is FailureCategory.UNKNOWN
    assert gate_calls == 1
    assert "gate" in sb.events
    assert "click_bound_confirm" in sb.events


class _Keyboard:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def type(self, text: str, delay: int = 10) -> None:
        self.calls.append(text)


class _BackendPage:
    def __init__(self, keyboard: _Keyboard) -> None:
        self.keyboard = keyboard


class _EnginePage:
    def __init__(self, keyboard: _Keyboard) -> None:
        self.backend_page = _BackendPage(keyboard)


class _Page:
    def __init__(self, keyboard: _Keyboard) -> None:
        self.engine_page = _EnginePage(keyboard)


class _StageSB:
    def __init__(self) -> None:
        self.keyboard = _Keyboard()
        self._page = _Page(self.keyboard)


@pytest.mark.parametrize(
    ("method_name", "kind"),
    [("fill_reply_composer", "reply"), ("fill_quote_composer", "quote")],
)
async def test_reply_quote_fill_refuse_when_kill_already_tripped(
    tmp_path: Path,
    method_name: str,
    kind: str,
) -> None:
    sb = _StageSB()
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    broker = M5ScopedWriteBroker(sb, kill)  # type: ignore[arg-type]
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = kind
    broker._m5_context_target = "123"
    kill.trip()

    result = await getattr(broker, method_name)("approved text")

    assert not result.ok
    assert result.failure_category is FailureCategory.SECURITY
    assert sb.keyboard.calls == []


@pytest.mark.parametrize(
    ("method_name", "kind"),
    [("fill_reply_composer", "reply"), ("fill_quote_composer", "quote")],
)
async def test_reply_quote_fill_rechecks_kill_after_focus(
    tmp_path: Path,
    method_name: str,
    kind: str,
) -> None:
    sb = _StageSB()
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    broker = M5ScopedWriteBroker(sb, kill)  # type: ignore[arg-type]
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = kind
    broker._m5_context_target = "123"

    async def focus_and_trip(self: M5ScopedWriteBroker) -> ActionResult:
        kill.trip()
        return ok_result(data={"focused": True})

    broker._focus_bound_textarea = MethodType(focus_and_trip, broker)  # type: ignore[method-assign]

    result = await getattr(broker, method_name)("approved text")

    assert not result.ok
    assert result.failure_category is FailureCategory.SECURITY
    assert sb.keyboard.calls == []


class _CleanupSB:
    def __init__(self, *, cleanup_ok: bool) -> None:
        self.cleanup_ok = cleanup_ok
        self.events: list[str] = []

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append(f"navigate:{url}")
        if self.cleanup_ok:
            return ok_result(data={"url": url})
        return soft_failure(
            "cleanup navigation failed",
            failure_category=FailureCategory.NAVIGATION,
        )


@pytest.mark.parametrize("cleanup_ok", [True, False])
async def test_cleanup_after_kill_releases_lease_only_on_success(
    tmp_path: Path,
    cleanup_ok: bool,
) -> None:
    sb = _CleanupSB(cleanup_ok=cleanup_ok)
    kill = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    broker = M5LeasedWriteBroker(sb, kill)  # type: ignore[arg-type]
    broker._m5_context_token = "ctx"
    broker._m5_context_kind = "post"
    broker._m5_context_target = "none"
    broker._m5_write_state.content_owner = broker._m5_lease_owner
    kill.trip()

    result = await broker.close_composer()

    assert result.ok is cleanup_ok
    assert sb.events == ["navigate:https://x.com/home"]
    if cleanup_ok:
        assert broker._m5_write_state.content_owner is None
        assert broker._m5_context_token is None
    else:
        assert broker._m5_write_state.content_owner == broker._m5_lease_owner
        assert broker._m5_context_token == "ctx"
