"""Tests for the read-only broker — URL guard, kill-switch guard, allowlist.

Uses a fake SuperBrowser so no real browser is needed. The broker only calls
``navigate/observe/extract/list_tabs/switch_tab/reload/go_back/go_forward``,
so the fake stubs exactly those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from super_browser.results.types import ActionResult, SuccessCategory
from webwire.broker import ReadOnlyBroker
from webwire.config import WebWireConfig
from webwire.envelope import ok_result
from webwire.safety import KillSwitch


class FakeSB:
    """Minimal stub of the SuperBrowser facade for broker tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []
        self.observe_data: dict[str, Any] = {"url": "https://x.com/home", "title": "Home"}

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> ActionResult:
        self.calls.append(("navigate", (url,), {"wait_until": wait_until}))
        return ok_result(data={"url": url}, success_category=SuccessCategory.NAVIGATION)

    async def reload(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        self.calls.append(("reload", (), {"wait_until": wait_until}))
        return ok_result()

    async def go_back(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        return ok_result()

    async def go_forward(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        return ok_result()

    async def observe(self) -> ActionResult:
        self.calls.append(("observe", (), {}))
        return ok_result(data=self.observe_data, success_category=SuccessCategory.INSPECTION)

    async def extract(self, query: str, *, selector: Optional[str] = None, schema: Optional[dict] = None) -> ActionResult:
        return ok_result(data={"handle": "test"})

    async def list_tabs(self) -> ActionResult:
        return ok_result(data=[])

    async def switch_tab(self, tab_id: int) -> ActionResult:
        return ok_result()


@pytest.fixture
def broker(tmp_path: Path) -> tuple[ReadOnlyBroker, FakeSB, KillSwitch]:
    sb = FakeSB()
    ks = KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    b = ReadOnlyBroker(sb, ks, WebWireConfig(state_dir=tmp_path))
    return b, sb, ks


async def test_broker_navigate_allows_x_surface(broker) -> None:
    b, sb, _ = broker
    r = await b.navigate("https://x.com/home")
    assert r.ok is True
    assert sb.calls[0][0] == "navigate"


async def test_broker_navigate_blocks_off_surface(broker) -> None:
    b, sb, _ = broker
    r = await b.navigate("https://evil.example.com/")
    assert r.ok is False
    assert r.failure_category.value == "security"
    # Underlying facade must NOT have been called.
    assert all(c[0] != "navigate" for c in sb.calls)


async def test_broker_navigate_blocks_non_http(broker) -> None:
    b, _, _ = broker
    r = await b.navigate("file:///etc/passwd")
    assert r.ok is False


async def test_broker_kill_switch_guard(broker) -> None:
    b, sb, ks = broker
    ks.trip()
    r = await b.navigate("https://x.com/home")
    assert r.ok is False
    assert r.failure_category.value == "security"
    assert all(c[0] != "navigate" for c in sb.calls)  # facade not touched


async def test_broker_observe_passes_through(broker) -> None:
    b, _, _ = broker
    r = await b.observe()
    assert r.ok is True
    assert r.data["url"] == "https://x.com/home"


async def test_broker_allowlist_is_fixed(broker) -> None:
    b, _, _ = broker
    allowed = b.allowed_methods
    assert "navigate" in allowed
    assert "observe" in allowed
    assert "extract" in allowed
    # Mutating methods must NOT be in the allowlist.
    for forbidden in ("click", "fill", "act", "delegate", "upload_file", "download", "check", "uncheck", "type_text"):
        assert forbidden not in allowed


async def test_broker_twitter_surface_allowed(broker) -> None:
    """twitter.com is also an allowed prefix (redirects to x.com but still)."""
    b, _, _ = broker
    r = await b.navigate("https://twitter.com/home")
    assert r.ok is True
