"""M5 Layer-4 exact-boundary tests for the concrete broker seam."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any, Optional

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.kill_switch import KillSwitch

URL = "https://x.com/u/status/123"
TARGET = "123"
DECOY = "999"


class _PostState:
    def __init__(self, *, liked: bool = False, bookmarked: bool = False) -> None:
        self.liked = liked
        self.bookmarked = bookmarked


class _MultiArticlePage:
    """Two-article model with the decoy logically appearing before the target."""

    def __init__(self) -> None:
        self.posts: dict[str, _PostState] = {
            DECOY: _PostState(liked=False, bookmarked=False),
            TARGET: _PostState(liked=False, bookmarked=False),
        }
        self.events: list[tuple[str, str]] = []
        self.target_present = True

    def state(self, post_id: str, kind: str) -> str:
        if not self.target_present and post_id == TARGET:
            return "target_missing"
        post = self.posts[post_id]
        if kind == "like":
            return "liked" if post.liked else "not_liked"
        return "bookmarked" if post.bookmarked else "not_bookmarked"

    def click(self, post_id: str, control: str) -> str:
        if not self.target_present and post_id == TARGET:
            return "target_missing"
        post = self.posts[post_id]
        if control == "like":
            post.liked = True
        elif control == "unlike":
            post.liked = False
        elif control == "bookmark":
            post.bookmarked = True
        elif control == "removeBookmark":
            post.bookmarked = False
        else:
            return "control_missing"
        self.events.append(("target_click", f"{post_id}:{control}"))
        return "clicked"


class _CDP:
    _MARKER_RE = re.compile(r"m5-target-(state|click)-([^:*]+):([0-9]+)")

    def __init__(self, page: _MultiArticlePage) -> None:
        self.page = page

    async def evaluate(self, expr: str) -> ActionResult:
        marker = self._MARKER_RE.search(expr)
        if marker is not None:
            mode, kind, post_id = marker.groups()
            assert "querySelectorAll('article')" in expr
            assert "querySelector('time')" in expr
            assert "endsWith('/status/'+targetId)" in expr
            if mode == "state":
                state_kind = "like" if kind == "like_state" else "bookmark"
                self.page.events.append(("target_probe", f"{post_id}:{state_kind}"))
                value = self.page.state(post_id, state_kind)
                return ok_result(data={"result": {"value": value}})
            self.page.events.append(("target_click_attempt", f"{post_id}:{kind}"))
            value = self.page.click(post_id, kind)
            return ok_result(data={"result": {"value": value}})
        return ok_result(data={"result": {"value": None}})


class _Controller:
    def __init__(self, page: _MultiArticlePage) -> None:
        self._cdp = _CDP(page)


class _SB:
    def __init__(self, page: _MultiArticlePage) -> None:
        self.page = page
        self._controller = _Controller(page)
        self.submit_clicks = 0

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.page.events.append(("navigate", url))
        return ok_result(data={"url": url})

    async def click(self, selector: str, description: str = "") -> ActionResult:
        if selector == "[data-testid='tweetButton']":
            self.submit_clicks += 1
            self.page.events.append(("submit_click", selector))
            return ok_result(data={"clicked": True})
        return soft_failure(f"unexpected global click {selector}")


async def _no_sleep(*args: Any, **kwargs: Any) -> None:
    return None


def _broker(tmp_path: Path, page: _MultiArticlePage) -> M5WriteBroker:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return M5WriteBroker(_SB(page), KillSwitch(cfg))  # type: ignore[arg-type]


async def test_like_targets_approved_article_not_first_decoy(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    # Make the decoy already liked: page-global state/click logic would observe
    # or act on the wrong semantic target.
    page.posts[DECOY].liked = True
    broker = _broker(tmp_path, page)

    def gate() -> Optional[ActionResult]:
        page.events.append(("gate", "like"))
        return None

    result = await broker.click_like(URL, _commit_gate=gate)

    assert result.ok
    assert page.posts[TARGET].liked is True
    assert page.posts[DECOY].liked is True
    assert page.events == [
        ("navigate", URL),
        ("target_probe", "123:like"),
        ("gate", "like"),
        ("target_click_attempt", "123:like"),
        ("target_click", "123:like"),
    ]


async def test_like_already_satisfied_is_zero_mutation_zero_gate(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    page.posts[TARGET].liked = True
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await broker.click_like(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert result.ok
    assert (result.data or {}).get("note") == "already_liked"
    assert gates == []
    assert page.events == [
        ("navigate", URL),
        ("target_probe", "123:like"),
    ]


async def test_unlike_targets_only_approved_article_and_is_retry_safe(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    page.posts[TARGET].liked = True
    page.posts[DECOY].liked = True
    broker = _broker(tmp_path, page)

    def gate() -> Optional[ActionResult]:
        page.events.append(("gate", "unlike"))
        return None

    first = await broker.click_unlike(URL, _commit_gate=gate)
    assert first.ok
    assert page.posts[TARGET].liked is False
    assert page.posts[DECOY].liked is True

    page.events.clear()
    second = await broker.click_unlike(URL, _commit_gate=gate)
    assert second.ok
    assert page.events == [
        ("navigate", URL),
        ("target_probe", "123:like"),
    ]
    assert page.posts[DECOY].liked is True


async def test_bookmark_targets_approved_article_after_probe(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(M5WriteBroker, "_BOOKMARK_SETTLE_S", 0.0)
    page = _MultiArticlePage()
    page.posts[DECOY].bookmarked = True
    broker = _broker(tmp_path, page)

    def gate() -> Optional[ActionResult]:
        page.events.append(("gate", "bookmark"))
        return None

    result = await broker.click_bookmark(URL, _commit_gate=gate)

    assert result.ok
    assert page.posts[TARGET].bookmarked is True
    assert page.posts[DECOY].bookmarked is True
    assert page.events == [
        ("navigate", URL),
        ("target_probe", "123:bookmark"),
        ("gate", "bookmark"),
        ("target_click_attempt", "123:bookmark"),
        ("target_click", "123:bookmark"),
    ]


async def test_state_set_without_scoped_gate_fails_before_target_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    result = await _broker(tmp_path, page).click_like(URL)

    assert not result.ok
    assert page.posts[TARGET].liked is False
    assert page.events == [
        ("navigate", URL),
        ("target_probe", "123:like"),
    ]


async def test_missing_target_article_fails_without_gate_or_mutation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    page.target_present = False
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    result = await broker.click_bookmark(
        URL,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert not result.ok
    assert gates == []
    assert page.posts[TARGET].bookmarked is False


async def test_submit_requires_scoped_payload_check_before_gate_and_click(
    tmp_path: Path
) -> None:
    page = _MultiArticlePage()
    broker = _broker(tmp_path, page)

    missing = await broker.click_submit(_commit_gate=lambda: None)
    assert not missing.ok
    assert page.events == []

    events: list[str] = []

    async def check() -> Optional[ActionResult]:
        events.append("check")
        return None

    def gate() -> Optional[ActionResult]:
        events.append("gate")
        return None

    ok = await broker.click_submit(_precommit_check=check, _commit_gate=gate)
    assert ok.ok
    assert events == ["check", "gate"]
    assert page.events == [("submit_click", "[data-testid='tweetButton']")]


async def test_submit_payload_check_denial_prevents_gate_and_click(tmp_path: Path) -> None:
    page = _MultiArticlePage()
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    async def deny() -> Optional[ActionResult]:
        return soft_failure("payload changed")

    result = await broker.click_submit(
        _precommit_check=deny,
        _commit_gate=lambda: gates.append("gate") or None,
    )

    assert not result.ok
    assert gates == []
    assert page.events == []


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

    result = await broker.delete_post(URL, TARGET, _commit_gate=gate)
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


async def test_delete_url_id_mismatch_fails_before_staging(tmp_path: Path) -> None:
    events: list[str] = []
    broker = _DeleteBroker(tmp_path, events)
    result = await broker.delete_post(URL, "999", _commit_gate=lambda: None)
    assert not result.ok
    assert events == []
