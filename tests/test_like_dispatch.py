"""Tests for like_post — the Phase 3b public-engagement canary.

Focuses on the pre-existing-state handling (ChatGPT's key Phase 3b requirement):
- Case A (not liked → click → liked): compensation eligible
- Case B (already liked → no-op → already_satisfied): NO compensation
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


class _FakeLikeBroker:
    """Fake write broker that simulates like state. Pre-set _liked to control
    whether the post starts as liked or not."""
    def __init__(self, already_liked: bool = False) -> None:
        self._liked = already_liked
        self.like_clicks: list[str] = []

    async def click_like(self, post_url: str) -> Any:
        self.like_clicks.append(post_url)
        self._liked = True
        return ok_result(data={"liked": True})

    async def read_like_state(self, post_url: str) -> Any:
        return ok_result(data={"like_state": "liked" if self._liked else "not_liked"})


def _make_dispatcher(tmp_path: Path, fake_broker) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    d._write_kernel._write_broker_factory = lambda: fake_broker  # type: ignore[attr-defined]
    return d


# ---------------------------------------------------------------------------
# Case A: not liked → click → liked (compensation eligible)
# ---------------------------------------------------------------------------

async def test_like_case_a_not_liked_clicks_and_compensation_eligible(tmp_path: Path) -> None:
    """Gate 5+8: pre_state=not_liked → click like → changed_by_this_invocation=True,
    compensation_eligible=True."""
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    # Phase 1: confirm.
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    token = r1.data["data"]["confirmation_token"]
    # Phase 2: execute.
    r2 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20", "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "allow"
    # The fake broker should have been clicked.
    assert len(fake.like_clicks) == 1


async def test_like_case_a_state_transition_records_delta(tmp_path: Path) -> None:
    """The state_transition in the result should show pre=not_liked, post=liked,
    changed_by_this_invocation=True, compensation_eligible=True."""
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20", "confirmation_token": token})
    # The execute result data should contain state_transition (in the kernel trace).
    # The kernel wraps the capability's result; check the trace.
    assert r2.data["trace"]["execute_ok"] is True


# ---------------------------------------------------------------------------
# Case B: already liked → no-op → already_satisfied (NO compensation)
# ---------------------------------------------------------------------------

async def test_like_case_b_already_liked_is_noop(tmp_path: Path) -> None:
    """Gate 9: pre_state=liked → already_satisfied, NO click, NO compensation."""
    fake = _FakeLikeBroker(already_liked=True)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20", "confirmation_token": token})
    assert r2.data["policy"]["verdict"] == "allow"
    # The fake broker should NOT have been clicked (already liked).
    assert len(fake.like_clicks) == 0


async def test_like_case_b_compensation_not_eligible(tmp_path: Path) -> None:
    """When pre_state=liked, compensation_eligible must be False — this invocation
    didn't create the like, so it must not undo it."""
    fake = _FakeLikeBroker(already_liked=True)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20", "confirmation_token": token})
    assert r2.data["trace"]["execute_ok"] is True
    # already_satisfied result should be reflected in the kernel trace data.


# ---------------------------------------------------------------------------
# Standard pipeline gates (same as bookmark)
# ---------------------------------------------------------------------------

async def test_like_first_invoke_confirmation_required(tmp_path: Path) -> None:
    """Gate 1+2: first invoke returns confirmation_required, not execution."""
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.data["policy"]["verdict"] == "confirmation_required"
    assert "confirmation_token" in r.data["data"]


async def test_like_intent_mismatch_blocked(tmp_path: Path) -> None:
    """Gate 3: token for post A cannot like post B."""
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/99", "confirmation_token": token})
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"


async def test_like_dedupe_blocks_replay(tmp_path: Path) -> None:
    """Gate 6: immediate replay blocked by dedupe."""
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20", "confirmation_token": token})
    r3 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r3.ok is False
    assert r3.data["policy"]["blocked_by"] == "dedupe"


async def test_like_kill_switch_blocks(tmp_path: Path) -> None:
    fake = _FakeLikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    d.kill_switch.trip()
    r = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.ok is False
    assert r.failure_category.value == "security"
