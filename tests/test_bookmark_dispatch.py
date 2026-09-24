"""Dispatcher-level tests for the M5-migrated bookmark_post canary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.session import SessionManager


class _StubSB:
    """Bare session facade; canary mutation is provided by the fake M5 broker."""

    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True
        self.set_resolved_handle("@actor")


class _FakeM5BookmarkBroker:
    """Stateful bookmark seam that requires the private commit callback."""

    def __init__(self) -> None:
        self.bookmark_clicks: list[str] = []
        self._bookmarked_posts: set[str] = set()

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> Any:
        if post_url in self._bookmarked_posts:
            return ok_result(data={"bookmarked": True, "result": "already_satisfied"})
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.bookmark_clicks.append(post_url)
        self._bookmarked_posts.add(post_url)
        return ok_result(data={"bookmarked": True})

    async def read_bookmark_state(self, post_url: str) -> Any:
        state = "bookmarked" if post_url in self._bookmarked_posts else "not_bookmarked"
        return ok_result(data={"bookmark_state": state})


def _install_fake_m5(d: Dispatcher, cfg: WebWireConfig) -> _FakeM5BookmarkBroker:
    fake = _FakeM5BookmarkBroker()
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=d._kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(
        fake,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    executor = M5EffectExecutor(runtime=runtime, evidence_reader=fake)  # type: ignore[arg-type]
    d._m5_stack = SimpleNamespace(effect_executor=executor)  # type: ignore[assignment]
    d._m5_canary_adapters.clear()
    return fake


@pytest.fixture
def dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker

    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    fake = _install_fake_m5(d, cfg)
    d._test_m5_bookmark_broker = fake  # type: ignore[attr-defined]
    # Migrated adapter must ignore this legacy factory entirely.
    d._write_kernel._write_broker_factory = lambda: object()  # type: ignore[attr-defined]
    return d


async def test_bookmark_first_invoke_returns_confirmation_required(dispatcher) -> None:
    r = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    assert r.ok is True
    policy = r.data["policy"]
    assert policy["verdict"] == "confirmation_required"
    assert "confirmation_token" in r.data["data"]
    assert "preview" in r.data["data"]


async def test_bookmark_full_pipeline_executes(dispatcher) -> None:
    r1 = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["policy"]["verdict"] == "allow"
    assert r2.data["trace"]["execute_ok"] is True
    fake = dispatcher._test_m5_bookmark_broker
    assert fake.bookmark_clicks == ["https://x.com/jack/status/20"]


async def test_bookmark_intent_mismatch_blocked(dispatcher) -> None:
    r1 = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/99", "confirmation_token": token},
    )
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"


async def test_bookmark_dedupe_blocks_replay(dispatcher) -> None:
    r1 = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    token = r1.data["data"]["confirmation_token"]
    await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    r2 = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "dedupe"


async def test_bookmark_confirmation_token_replay_after_execution(dispatcher) -> None:
    r1 = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["policy"]["verdict"] == "allow"
    r3 = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r3.ok is False
    assert r3.data["policy"]["blocked_by"] in ("consumed_token", "dedupe")


async def test_bookmark_kill_switch_blocks(dispatcher) -> None:
    dispatcher.kill_switch.trip()
    r = await dispatcher.invoke(
        "bookmark_post", {"post_url": "https://x.com/jack/status/20"}
    )
    assert r.ok is False
    assert r.failure_category.value == "security"
