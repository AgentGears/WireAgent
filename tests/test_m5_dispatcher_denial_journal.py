"""Audit regressions for Dispatcher-level M5 write denials."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.session import SessionManager


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._started = True


class _ReadBroker:
    pass


class _FutureWriteCapability:
    name = "future_write"
    tier = CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> Any:
        raise AssertionError("dispatcher denial must happen before compose")


def _records(cfg: WebWireConfig) -> list[dict[str, Any]]:
    path = cfg.journal_path()
    assert path.exists()
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _assert_one_denied_write(cfg: WebWireConfig, capability: str) -> None:
    records = _records(cfg)
    assert len(records) == 1
    record = records[0]
    assert record["capability"] == capability
    assert record["policy_decision"] == "denied"
    assert record["result_ok"] is False
    assert record["failure_category"] == "security"
    assert record["capability_tier"] == "write"
    assert record["action_type"] is None
    assert record["dedupe_key"] is None


async def test_missing_actor_denial_journals_once_as_denied(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=session)
    dispatcher._broker = _ReadBroker()  # type: ignore[assignment]
    dispatcher._m5_stack = object()  # type: ignore[assignment]

    result = await dispatcher.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/u/status/123"},
    )

    assert result.ok is False
    assert "whoami-resolved actor identity" in result.error.message
    _assert_one_denied_write(cfg, "bookmark_post")


async def test_missing_stack_denial_journals_once_as_denied(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)
    dispatcher._broker = _ReadBroker()  # type: ignore[assignment]
    assert dispatcher._m5_stack is None

    result = await dispatcher.invoke(
        "like_post",
        {"post_url": "https://x.com/u/status/123"},
    )

    assert result.ok is False
    assert "authority stack is not installed" in result.error.message
    _assert_one_denied_write(cfg, "like_post")


async def test_unmigrated_write_denial_journals_once_as_denied(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StubSessionManager(cfg)
    session.set_resolved_handle("@actor")
    dispatcher = Dispatcher(cfg, session_manager=session)
    dispatcher._broker = _ReadBroker()  # type: ignore[assignment]
    dispatcher._registry.register(_FutureWriteCapability())  # type: ignore[arg-type]

    result = await dispatcher.invoke("future_write", {})

    assert result.ok is False
    assert "unmigrated WRITE capability" in result.error.message
    _assert_one_denied_write(cfg, "future_write")
