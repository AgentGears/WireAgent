"""Layer-5 bypass regressions for the supported Dispatcher WRITE surface."""

from __future__ import annotations

from pathlib import Path

import webwire.dispatcher as dispatcher_module
from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.session import SessionManager
from webwire.write_broker import WriteBroker


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._started = True


class _ReadBroker:
    pass


class _FutureWriteCapability:
    name = "future_write"
    tier = CapabilityTier.WRITE

    def __init__(self) -> None:
        self.compose_calls = 0

    def compose(self, input, actor_identity):  # type: ignore[no-untyped-def]
        self.compose_calls += 1
        raise AssertionError("unmigrated WRITE must be denied before compose")


async def test_registered_remote_write_surface_is_fully_m5_migrated(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    dispatcher = Dispatcher(cfg, session_manager=_StubSessionManager(cfg))

    write_names = {
        name
        for name in dispatcher.capabilities
        if dispatcher._registry.get(name).tier == CapabilityTier.WRITE  # type: ignore[union-attr]
    }

    assert write_names - dispatcher_module._M5_MIGRATED_CAPABILITIES == {
        dispatcher_module._M5_DRY_RUN_CAPABILITY
    }


async def test_write_kernel_factory_no_longer_constructs_legacy_write_broker(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    dispatcher = Dispatcher(cfg, session_manager=_StubSessionManager(cfg))

    broker = dispatcher._write_kernel._write_broker_factory()

    assert not isinstance(broker, WriteBroker)
    try:
        _ = getattr(broker, "delete_post")
    except RuntimeError as exc:
        assert "legacy mutation broker surface is disabled" in str(exc)
    else:  # pragma: no cover - fail loudly if authority reappears
        raise AssertionError("inert kernel broker exposed a mutation attribute")


async def test_future_unmigrated_write_is_security_denied_before_compose(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)
    dispatcher._broker = _ReadBroker()  # type: ignore[assignment]
    capability = _FutureWriteCapability()
    dispatcher._registry.register(capability)  # type: ignore[arg-type]

    result = await dispatcher.invoke("future_write", {})

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert "unmigrated WRITE capability" in result.error.message
    assert capability.compose_calls == 0


async def test_compose_post_dry_run_still_works_with_inert_kernel_broker(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)
    dispatcher._broker = _ReadBroker()  # type: ignore[assignment]

    first = await dispatcher.invoke("compose_post", {"text": "dry run only"})
    assert first.data["policy"]["verdict"] == "confirmation_required"
    token = first.data["data"]["confirmation_token"]

    second = await dispatcher.invoke(
        "compose_post",
        {"text": "dry run only", "confirmation_token": token},
    )

    assert second.data["policy"]["verdict"] == "allow"
    assert second.data["data"]["dry_run"] is True
    assert second.data["trace"]["dedupe_recorded"] is False
