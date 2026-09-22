"""Tests for the dispatcher — unsupported-capability journaling + kill-switch
top-of-dispatcher guard, WITHOUT a real browser.

Uses a stub SessionManager whose ``sb`` is a bare object (enough to construct
a broker) and whose start()/stop() succeed without touching Chrome.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.session import SessionManager


class _StubSB:
    """Bare stand-in so ReadOnlyBroker can be constructed. Its methods are
    never reached in these tests because the guards fire first."""


class _StubSessionManager(SessionManager):
    """SessionManager that fakes a successful attach without Chrome."""

    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


@pytest.fixture
def dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    # Bypass start() (which would try to attach); broker already constructable.
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    d._register_defaults()
    return d


async def test_dispatch_unsupported_capability_returns_validation(tmp_path, dispatcher) -> None:
    r = await dispatcher.invoke("like", {"post_url": "https://x.com/foo"})
    assert r.ok is False
    assert r.failure_category.value == "validation"
    assert "like" in (r.error.message if r.error else "")


async def test_dispatch_unsupported_is_journaled(tmp_path, dispatcher) -> None:
    await dispatcher.invoke("like")
    journal_path = tmp_path / "journal.ndjson"
    assert journal_path.exists()
    line = journal_path.read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["capability"] == "like"
    assert rec["policy_decision"] == "unsupported"
    assert rec["failure_category"] == "validation"


async def test_dispatch_kill_switch_blocks_at_top(tmp_path, dispatcher) -> None:
    dispatcher.kill_switch.trip()
    r = await dispatcher.invoke("whoami")
    assert r.ok is False
    assert r.failure_category.value == "security"
    # Journal records the kill.
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["policy_decision"] == "killed"
    assert rec["kill_switch_tripped"] is True


async def test_dispatch_kill_dominates_unsupported(tmp_path, dispatcher) -> None:
    """Review-iteration change: kill switch now checked BEFORE unsupported.
    A tripped kill must return 'killed' even for an unsupported capability
    name, so the operator invariant ('killed => every invoke returns killed')
    is absolute."""
    dispatcher.kill_switch.trip()
    r = await dispatcher.invoke("like")  # unsupported cap, but kill dominates
    assert r.ok is False
    assert r.failure_category.value == "security"  # NOT validation
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["policy_decision"] == "killed"  # NOT unsupported
    assert rec["kill_switch_tripped"] is True


async def test_dispatch_unsupported_journaled_when_not_killed(tmp_path, dispatcher) -> None:
    """When kill is clear, unsupported still returns validation as before."""
    assert not dispatcher.kill_switch.tripped()
    r = await dispatcher.invoke("like")
    assert r.ok is False
    assert r.failure_category.value == "validation"
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["policy_decision"] == "unsupported"


async def test_dispatch_lists_registered_capabilities(dispatcher) -> None:
    caps = dispatcher.capabilities
    assert "whoami" in caps
    assert "health" in caps
    # No write capabilities registered.
    for forbidden in ("like", "bookmark", "reply", "tweet", "follow", "repost"):
        assert forbidden not in caps


async def test_dispatch_journal_redacts_target_url(tmp_path, dispatcher) -> None:
    # Invoke an unsupported cap with a URL containing a query — should be redacted.
    await dispatcher.invoke(
        "like",
        {"post_url": "https://x.com/foo/status/123?secret=abc&token=xyz"},
    )
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    # target keeps origin+path, drops query
    assert rec["target"] == "https://x.com/foo/status/123"
    assert "secret" not in line
    assert "token" not in line
