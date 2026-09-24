"""Dispatcher construction and routing guards for Layer-5 canaries."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.session import SessionManager


class _StubSB:
    pass


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


async def test_start_installs_m5_live_stack_before_canary_use(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]

    result = await dispatcher.start()

    assert result.ok is True
    assert dispatcher._broker is not None
    assert dispatcher._m5_stack is not None
    assert dispatcher._m5_stack.write_broker._kill is dispatcher.kill_switch
    assert dispatcher._m5_gateway._kill is dispatcher.kill_switch


async def test_migrated_canary_denies_without_whoami_actor(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    started = await dispatcher.start()
    assert started.ok is True
    assert session.resolved_handle is None

    result = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/u/status/123"},
    )

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "whoami-resolved actor identity" in result.error.message


async def test_migrated_canary_denies_if_live_stack_disappears(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    started = await dispatcher.start()
    assert started.ok is True

    dispatcher._m5_stack = None
    result = await dispatcher.invoke(
        "like_post",
        {"post_url": "https://x.com/u/status/123"},
    )

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "authority stack is not installed" in result.error.message


async def test_confirmation_phase_caches_m5_adapter_not_registry_replacement(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    started = await dispatcher.start()
    assert started.ok is True
    original = dispatcher._registry.get("bookmark_post")
    assert original is not None

    result = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/u/status/123"},
    )

    assert result.ok is True
    assert result.data["policy"]["verdict"] == "confirmation_required"
    adapter = dispatcher._m5_canary_adapters["bookmark_post"]
    assert adapter is not original
    assert dispatcher._registry.get("bookmark_post") is original


async def test_start_fails_closed_when_live_stack_build_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]

    def fail_build(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("stack build failed")

    monkeypatch.setattr(
        "webwire.safety.m5_live_runtime.build_live_m5_execution_stack",
        fail_build,
    )

    result = await dispatcher.start()

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "M5 live execution stack initialization failed" in result.error.message
    assert dispatcher._broker is None
    assert dispatcher._m5_stack is None
