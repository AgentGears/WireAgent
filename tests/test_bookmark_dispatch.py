"""Dispatcher-level tests for bookmark_post — the Phase 3 canary write.

Tests the write pipeline at the DISPATCHER level (not just kernel level),
including ChatGPT's specifically-requested confirmation-token replay test.

Uses a stubbed session (no real browser) with a FakeWriteBroker that records
clicks, so the pipeline mechanics are testable without live X.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.session import SessionManager
from webwire.write_broker import WriteBroker


class _StubSB:
    """Stand-in for SuperBrowser. The FakeWriteBroker records its calls."""
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


class _FakeWriteBroker:
    """Records bookmark clicks + bookmark-state reads. Pretends to be a WriteBroker."""
    def __init__(self) -> None:
        self.bookmark_clicks: list[str] = []
        self._bookmarked_posts: set[str] = set()

    async def click_bookmark(self, post_url: str) -> Any:
        self.bookmark_clicks.append(post_url)
        self._bookmarked_posts.add(post_url)
        from webwire.envelope import ok_result
        return ok_result(data={"bookmarked": True})

    async def read_bookmark_state(self, post_url: str) -> Any:
        from webwire.envelope import ok_result
        state = "bookmarked" if post_url in self._bookmarked_posts else "not_bookmarked"
        return ok_result(data={"bookmark_state": state})


@pytest.fixture
def dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    # Override the WriteBroker factory with a fake that records clicks.
    fake_wb = _FakeWriteBroker()
    d._write_kernel._write_broker_factory = lambda: fake_wb  # type: ignore[attr-defined]
    return d


async def test_bookmark_first_invoke_returns_confirmation_required(dispatcher) -> None:
    """Gate 4: first invoke must stop at confirmation_required."""
    r = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.ok is True
    policy = r.data["policy"]
    assert policy["verdict"] == "confirmation_required"
    assert "confirmation_token" in r.data["data"]
    assert "preview" in r.data["data"]


async def test_bookmark_full_pipeline_executes(dispatcher) -> None:
    """Gates 4+5: first invoke → confirm, second invoke with token → execute."""
    # Phase 1.
    r1 = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    # Phase 2.
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    policy = r2.data["policy"]
    assert policy["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True


async def test_bookmark_intent_mismatch_blocked(dispatcher) -> None:
    """Gate: token for post A cannot bookmark post B."""
    r1 = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/99", "confirmation_token": token},
    )
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"


async def test_bookmark_dedupe_blocks_replay(dispatcher) -> None:
    """Gate 8: immediate replay of the same bookmark is blocked by dedupe."""
    # Full pipeline once.
    r1 = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    # Replay — should be dedupe-blocked at phase 1 (before confirmation).
    r2 = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "dedupe"


async def test_bookmark_confirmation_token_replay_after_execution(dispatcher) -> None:
    """ChatGPT's specific request: confirmation-token replay after successful
    execution at the DISPATCHER level (not just kernel). The kernel already
    rejects consumed tokens, but the dispatcher integration is part of the
    trusted boundary."""
    # Execute.
    r1 = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["policy"]["verdict"] == "allow"
    # Replay the same token — should be blocked (by dedupe, since the action
    # was already executed and recorded).
    r3 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r3.ok is False
    # The replay is caught — either by consumed_token or dedupe (both are valid;
    # dedupe fires first because it's checked before token validation).
    assert r3.data["policy"]["blocked_by"] in ("consumed_token", "dedupe")


async def test_bookmark_kill_switch_blocks(dispatcher) -> None:
    """Kill switch blocks the bookmark at the dispatcher top (before reaching
    the kernel). The dispatcher-level kill returns a simple kill_switched()
    envelope — not the kernel's policy-structured DENY, because the dispatcher
    kill gate fires first (it's the coarser, earlier gate)."""
    dispatcher.kill_switch.trip()
    r = await dispatcher.invoke("bookmark_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.ok is False
    assert r.failure_category.value == "security"
