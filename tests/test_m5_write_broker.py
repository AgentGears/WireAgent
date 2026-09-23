"""M5 Layer-4 exact-boundary tests for the concrete broker seam."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.kill_switch import KillSwitch

URL = "https://x.com/u/status/123"
LIKE = "[data-testid='like']"
UNLIKE = "[data-testid='unlike']"
BOOKMARK = "[data-testid='bookmark']"
REMOVE_BOOKMARK = "[data-testid='removeBookmark']"


class _StatePage:
    """Selector model with today's coexist topology plus directional effects."""

    def __init__(self, *, liked: bool = False, bookmarked: bool = False) -> None:
        self.liked = liked
        self.bookmarked = bookmarked
        self.events: list[tuple[str, str]] = []

    def like_state(self) -> str:
        # Coexist topology: like is visible in both states; unlike marks liked.
        return "liked" if self.liked else "not_liked"

    def bookmark_state(self) -> str:
        return "bookmarked" if self.bookmarked else "not_bookmarked"

    def click(self, selector: str) -> ActionResult:
        self.events.append(("click", selector))
        if selector == LIKE:
            if not self.liked:
                self.liked = True
            return ok_result(data={"clicked": True})
        if selector == UNLIKE:
            if self.liked:
                self.liked = False
            return ok_result(data={"clicked": True})
        if selector == BOOKMARK:
            if not self.bookmarked:
                self.bookmarked = True
            return ok_result(data={"clicked": True})
        if selector == REMOVE_BOOKMARK:
            if self.bookmarked:
                self.bookmarked = False
            return ok_result(data={"clicked": True})
        if selector == "[data-testid='tweetButton']":
            return ok_result(data={"clicked": True})
        return soft_failure(f"unexpected selector {selector}")


class _CDP:
    def __init__(self, page: _StatePage) -> None:
        self.page = page

    async def evaluate(self, expr: str) -> ActionResult:
        if "unlike" in expr and "like" in expr:
            self.page.events.append(("probe", "like"))
            return ok_result(data={"result": {"value": self.page.like_state()}})
        if "removeBookmark" in expr and "bookmark" in expr:
            self.page.events.append(("probe", "bookmark"))
            return ok_result(data={"result": {"value": self.page.bookmark_state()}})
        return ok_result(data={"result": {"value": None}})


class _Controller:
    def __init__(self, page: _StatePage) -> None:
        self._cdp = _CDP(page)


class _SB:
    def __init__(self, page: _StatePage) -> None:
        self.page = page
        self._controller = _Controller(page)

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.page.events.append(("navigate", url))
        return ok_result(data={"url": url})

    async def click(self, selector: str, description: str = "") -> ActionResult:
        return self.page.click(selector)


def _broker(tmp_path: Path, page: _StatePage) -> M5WriteBroker:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return M5WriteBroker(_SB(page), KillSwitch(cfg))  # type: ignore[arg-type]


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


async def test_like_already_satisfied_consumes_no_gate_and_makes_no_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _StatePage(liked=True)
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    def gate() -> Optional[ActionResult]:
        gates.append("gate")
        return None

    result = await broker.click_like(URL, _commit_gate=gate)
    assert result.ok
    assert (result.data or {}).get("note") == "already_liked"
    assert gates == []
    assert page.events == [("navigate", URL), ("probe", "like")]
    assert page.liked is True


async def test_like_not_liked_crosses_gate_after_probe_before_directional_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _StatePage(liked=False)
    broker = _broker(tmp_path, page)

    def gate() -> Optional[ActionResult]:
        page.events.append(("gate", "like"))
        return None

    result = await broker.click_like(URL, _commit_gate=gate)
    assert result.ok
    assert page.events == [
        ("navigate", URL),
        ("probe", "like"),
        ("gate", "like"),
        ("click", LIKE),
    ]
    assert page.liked is True


async def test_unlike_is_directional_and_retry_safe(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)

    already_clear = _StatePage(liked=False)
    broker = _broker(tmp_path, already_clear)
    gates: list[str] = []
    result = await broker.click_unlike(
        URL, _commit_gate=lambda: gates.append("gate") or None
    )
    assert result.ok
    assert gates == []
    assert already_clear.events == [("navigate", URL), ("probe", "like")]

    liked = _StatePage(liked=True)
    broker2 = _broker(tmp_path, liked)

    def gate() -> Optional[ActionResult]:
        liked.events.append(("gate", "unlike"))
        return None

    first = await broker2.click_unlike(URL, _commit_gate=gate)
    assert first.ok
    assert liked.events[-2:] == [("gate", "unlike"), ("click", UNLIKE)]
    assert liked.liked is False

    # Replay of the same semantic state-set is a no-op: no second gate/click.
    liked.events.clear()
    second = await broker2.click_unlike(URL, _commit_gate=gate)
    assert second.ok
    assert liked.events == [("navigate", URL), ("probe", "like")]
    assert liked.liked is False


async def test_state_set_without_scoped_gate_fails_closed_before_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _StatePage(liked=False)
    result = await _broker(tmp_path, page).click_like(URL)
    assert not result.ok
    assert page.events == [("navigate", URL), ("probe", "like")]
    assert page.liked is False


async def test_bookmark_gate_is_after_state_probe_not_before_it(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _StatePage(bookmarked=False)
    broker = _broker(tmp_path, page)
    monkeypatch.setattr(M5WriteBroker, "_BOOKMARK_SETTLE_S", 0.0)

    def gate() -> Optional[ActionResult]:
        page.events.append(("gate", "bookmark"))
        return None

    result = await broker.click_bookmark(URL, _commit_gate=gate)
    assert result.ok
    assert page.events == [
        ("navigate", URL),
        ("probe", "bookmark"),
        ("gate", "bookmark"),
        ("click", BOOKMARK),
    ]


class _DeleteSB:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.events.append("navigate")
        return ok_result(data={"url": url})


class _DeleteBroker(M5WriteBroker):
    def __init__(self, tmp_path: Path, events: list[str]) -> None:
        cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
        super().__init__(_DeleteSB(events), KillSwitch(cfg))  # type: ignore[arg-type]
        self.events = events

    async def _delete_poll(self, expr_fn, *, want_true: bool, label: str) -> ActionResult:  # type: ignore[no-untyped-def]
        self.events.append(f"poll:{label}")
        return ok_result(data=True)

    async def _delete_eval(self, expr: str) -> ActionResult:
        if "caret_clicked" in expr:
            self.events.append("caret_click")
            return ok_result(data="caret_clicked")
        if "confirm_clicked" in expr:
            self.events.append("confirm_click")
            return ok_result(data="confirm_clicked")
        self.events.append("eval")
        return ok_result(data=True)

    async def _dismiss_delete_dialog(self) -> None:
        self.events.append("dismiss")


async def test_delete_gate_runs_only_after_confirmation_is_ready(tmp_path: Path) -> None:
    events: list[str] = []
    broker = _DeleteBroker(tmp_path, events)

    def gate() -> Optional[ActionResult]:
        events.append("gate")
        return None

    result = await broker.delete_post(URL, "123", _commit_gate=gate)
    assert result.ok
    assert events == [
        "navigate",
        "poll:target article",
        "caret_click",
        "poll:delete menu item",
        "poll:confirmation button",
        "gate",
        "confirm_click",
        "poll:confirmation sheet dismissed",
    ]


async def test_delete_gate_denial_prevents_confirmation_click(tmp_path: Path) -> None:
    events: list[str] = []
    broker = _DeleteBroker(tmp_path, events)

    def gate() -> Optional[ActionResult]:
        events.append("gate")
        return soft_failure("denied")

    result = await broker.delete_post(URL, "123", _commit_gate=gate)
    assert not result.ok
    assert "confirm_click" not in events
    assert events[-2:] == ["gate", "dismiss"]
