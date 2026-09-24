"""Dispatcher-level tests for the M5-migrated like_post canary."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True
        self.set_resolved_handle("@actor")


class _FakeM5LikeBroker:
    """Stateful REQUIRED-effect seam with an explicit private commit callback."""

    def __init__(self, already_liked: bool = False) -> None:
        self._liked = already_liked
        self.like_clicks: list[str] = []

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> Any:
        if self._liked:
            return ok_result(data={"liked": True, "note": "already_liked"})
        denied = _commit_gate()
        if denied is not None:
            return denied
        self.like_clicks.append(post_url)
        self._liked = True
        return ok_result(data={"liked": True})

    async def read_like_state(self, post_url: str) -> Any:
        del post_url
        return ok_result(data={"like_state": "liked" if self._liked else "not_liked"})


def _make_dispatcher(
    tmp_path: Path,
    fake_broker: _FakeM5LikeBroker,
) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker

    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=d._kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(
        fake_broker,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    executor = M5EffectExecutor(
        runtime=runtime,
        evidence_reader=fake_broker,  # type: ignore[arg-type]
    )
    d._m5_stack = SimpleNamespace(effect_executor=executor)  # type: ignore[assignment]
    d._m5_canary_adapters.clear()
    d._write_kernel._write_broker_factory = lambda: object()  # type: ignore[attr-defined]
    return d


async def test_like_case_a_not_liked_clicks_and_compensation_eligible(
    tmp_path: Path,
) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r1.data["policy"]["verdict"] == "confirmation_required"
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["policy"]["verdict"] == "allow"
    assert len(fake.like_clicks) == 1


async def test_like_case_a_records_required_effect_fact(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["trace"]["execute_ok"] is True
    assert [record.state for record in d._m5_ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]


async def test_like_case_b_already_liked_is_noop(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=True)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["policy"]["verdict"] == "allow"
    assert len(fake.like_clicks) == 0


async def test_like_case_b_has_no_effect_ledger_fact(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=True)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    assert r2.data["trace"]["execute_ok"] is True
    assert d._m5_ledger.read_records() == []


async def test_like_first_invoke_confirmation_required(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.data["policy"]["verdict"] == "confirmation_required"
    assert "confirmation_token" in r.data["data"]


async def test_like_intent_mismatch_blocked(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/99", "confirmation_token": token},
    )
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"


async def test_like_dedupe_blocks_replay(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    r1 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    token = r1.data["data"]["confirmation_token"]
    await d.invoke(
        "like_post",
        {"post_url": "https://x.com/jack/status/20", "confirmation_token": token},
    )
    r3 = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r3.ok is False
    assert r3.data["policy"]["blocked_by"] == "dedupe"


async def test_like_kill_switch_blocks(tmp_path: Path) -> None:
    fake = _FakeM5LikeBroker(already_liked=False)
    d = _make_dispatcher(tmp_path, fake)
    d.kill_switch.trip()
    r = await d.invoke("like_post", {"post_url": "https://x.com/jack/status/20"})
    assert r.ok is False
    assert r.failure_category.value == "security"
