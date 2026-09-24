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
    def __init__(self) -> None:
        self.posts: dict[str, _PostState] = {
            DECOY: _PostState(),
            TARGET: _PostState(),
        }
        self.events: list[tuple[str, str]] = []
        self.target_present = True
        self.submit_bind_result = "bound"
        self.submit_click_result = "clicked"

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
    _TARGET_RE = re.compile(r"m5-target-(state|click)-([^:*]+):([0-9]+)")
    _SUBMIT_BIND_RE = re.compile(r"m5-submit-bind:([0-9a-f]+)")
    _SUBMIT_CLICK_RE = re.compile(r"m5-submit-click:([0-9a-f]+)")

    def __init__(self, page: _MultiArticlePage) -> None:
        self.page = page

    async def evaluate(self, expr: str) -> ActionResult:
        marker = self._TARGET_RE.search(expr)
        if marker is not None:
            mode, kind, post_id = marker.groups()
            assert "querySelectorAll('article')" in expr
            assert "querySelector('time')" in expr
            assert "endsWith('/status/'+targetId)" in expr
            # Nested quote timestamps must not make the outer article authoritative.
            assert "a.closest('article')!==arts[ai]" in expr
            if mode == "state":
                state_kind = "like" if kind == "like_state" else "bookmark"
                self.page.events.append(("target_probe", f"{post_id}:{state_kind}"))
                return ok_result(data={"result": {"value": self.page.state(post_id, state_kind)}})
            self.page.events.append(("target_click_attempt", f"{post_id}:{kind}"))
            return ok_result(data={"result": {"value": self.page.click(post_id, kind)}})

        if self._SUBMIT_BIND_RE.search(expr):
            assert "matches.length!==1" in expr
            assert "data-wireagent-submit-root" in expr
            assert "data-wireagent-submit-button" in expr
            assert "tweetTextarea_0" in expr
            self.page.events.append(("submit_bind", "composer"))
            return ok_result(data={"result": {"value": self.page.submit_bind_result}})

        if self._SUBMIT_CLICK_RE.search(expr):
            assert "root.contains(btn)" in expr
            assert "payload_changed" in expr
            assert "attachments_changed" in expr
            self.page.events.append(("submit_bound_click", "composer"))
            return ok_result(data={"result": {"value": self.page.submit_click_result}})

        return ok_result(data={"result": {"value": None}})


class _Controller:
    def __init__(self, page: _MultiArticlePage) -> None:
        self._cdp = _CDP(page)


class _SB:
    def __init__(self, page: _MultiArticlePage) -> None:
        self.page = page
        self._controller = _Controller(page)
        self.global_clicks = 0

    async def navigate(self, url: str, wait_until: str = "domcontentloaded") -> ActionResult:
        self.page.events.append(("navigate", url))
        return ok_result(data={"url": url})

    async def click(self, selector: str, description: str = "") -> ActionResult:
        self.global_clicks += 1
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
    result = await broker.click_like(URL, _commit_gate=lambda: gates.append("gate") or None)
    assert result.ok
    assert gates == []
    assert page.events == [("navigate", URL), ("target_probe", "123:like")]


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

    assert (await broker.click_unlike(URL, _commit_gate=gate)).ok
    assert page.posts[TARGET].liked is False
    assert page.posts[DECOY].liked is True
    page.events.clear()
    assert (await broker.click_unlike(URL, _commit_gate=gate)).ok
    assert page.events == [("navigate", URL), ("target_probe", "123:like")]


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

    assert (await broker.click_bookmark(URL, _commit_gate=gate)).ok
    assert page.posts[TARGET].bookmarked is True
    assert page.posts[DECOY].bookmarked is True


async def test_state_set_without_scoped_gate_fails_before_target_click(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    result = await _broker(tmp_path, page).click_like(URL)
    assert not result.ok
    assert page.posts[TARGET].liked is False
    assert page.events == [("navigate", URL), ("target_probe", "123:like")]


async def test_missing_target_article_fails_without_gate_or_mutation(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    page = _MultiArticlePage()
    page.target_present = False
    gates: list[str] = []
    result = await _broker(tmp_path, page).click_bookmark(
        URL, _commit_gate=lambda: gates.append("gate") or None
    )
    assert not result.ok
    assert gates == []


async def test_submit_requires_exact_payload_binding_before_gate(tmp_path: Path) -> None:
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

    result = await broker.click_submit(
        _precommit_check=check,
        _commit_gate=gate,
        _expected_text="approved text",
        _expected_attachments=0,
    )
    assert result.ok
    assert events == ["check", "gate"]
    assert page.events == [
        ("submit_bind", "composer"),
        ("submit_bound_click", "composer"),
    ]
    assert broker._sb.global_clicks == 0  # type: ignore[attr-defined]


async def test_submit_ambiguous_composer_fails_before_gate(tmp_path: Path) -> None:
    page = _MultiArticlePage()
    page.submit_bind_result = "ambiguous"
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    async def check() -> Optional[ActionResult]:
        return None

    result = await broker.click_submit(
        _precommit_check=check,
        _commit_gate=lambda: gates.append("gate") or None,
        _expected_text="approved text",
        _expected_attachments=0,
    )
    assert not result.ok
    assert gates == []
    assert page.events == [("submit_bind", "composer")]


async def test_submit_bound_control_change_after_gate_is_unknown_not_fallback(
    tmp_path: Path,
) -> None:
    page = _MultiArticlePage()
    page.submit_click_result = "payload_changed"
    broker = _broker(tmp_path, page)
    gates: list[str] = []

    async def check() -> Optional[ActionResult]:
        return None

    result = await broker.click_submit(
        _precommit_check=check,
        _commit_gate=lambda: gates.append("gate") or None,
        _expected_text="approved text",
        _expected_attachments=0,
    )
    assert not result.ok
    assert gates == ["gate"]
    assert page.events[-1] == ("submit_bound_click", "composer")


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
        return await expr_fn()

    async def _delete_eval(self, expr: str) -> ActionResult:
        if "m5-delete-baseline:" in expr:
            self.events.append("baseline")
            return ok_result(data="baselined")
        if "caret_clicked" in expr:
            assert "closest('article')!==art" in expr
            self.events.append("caret_click")
            return ok_result(data="caret_clicked")
        if "m5-delete-bind-menu:" in expr:
            assert "data-wireagent-delete-baseline" in expr
            self.events.append("bind_menu")
            return ok_result(data="menu_bound")
        if "m5-delete-click-menu:" in expr:
            self.events.append("click_bound_menu_item")
            return ok_result(data="delete_item_clicked")
        if "m5-delete-bind-confirm:" in expr:
            self.events.append("bind_confirm")
            return ok_result(data="confirm_bound")
        if "m5-delete-click-confirm:" in expr:
            self.events.append("confirm_click")
            return ok_result(data="confirm_clicked")
        if "data-wireagent-delete-confirm" in expr:
            self.events.append("confirm_dismissed")
            return ok_result(data=True)
        self.events.append("target_found")
        return ok_result(data=True)

    async def _dismiss_delete_dialog(self) -> None:
        self.events.append("dismiss")


async def test_delete_gate_runs_only_after_exact_confirmation_is_bound(tmp_path: Path) -> None:
    events: list[str] = []
    broker = _DeleteBroker(tmp_path, events)

    def gate() -> Optional[ActionResult]:
        events.append("gate")
        return None

    result = await broker.delete_post(URL, TARGET, _commit_gate=gate)
    assert result.ok
    assert events == [
        "navigate",
        "baseline",
        "poll:target article",
        "target_found",
        "caret_click",
        "poll:bound delete menu",
        "bind_menu",
        "click_bound_menu_item",
        "poll:bound confirmation button",
        "bind_confirm",
        "gate",
        "confirm_click",
        "poll:confirmation sheet dismissed",
        "confirm_dismissed",
    ]


async def test_delete_url_id_mismatch_fails_before_staging(tmp_path: Path) -> None:
    events: list[str] = []
    broker = _DeleteBroker(tmp_path, events)
    result = await broker.delete_post(URL, "999", _commit_gate=lambda: None)
    assert not result.ok
    assert events == []
