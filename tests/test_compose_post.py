"""Tests for Phase 4a compose_post — dry-run compose safety machinery.

Covers the 7 Phase 4 invariants (ChatGPT's gate-level requirements):
1. Normalization is deterministic + stable.
2. Token binds normalized_text (intent_hash changes if text changes).
3. Execute is a NO-OP (dry-run only, no submit).
4. Dedupe key includes text_hash.
5. Length validation rejects >280 chars.
6. Preview text = execution text (same normalized_text).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.session import SessionManager


# -- text normalization ----------------------------------------------------

def test_normalize_strips_whitespace() -> None:
    assert normalize_text("  hello world  ") == "hello world"


def test_normalize_collapses_internal_spaces() -> None:
    assert normalize_text("hello    world") == "hello world"


def test_normalize_preserves_paragraph_breaks() -> None:
    assert normalize_text("line one\n\nline two") == "line one\n\nline two"


def test_normalize_line_endings() -> None:
    assert normalize_text("line one\r\nline two") == "line one\nline two"


def test_normalize_deterministic() -> None:
    """Same input always produces same output — critical for token binding."""
    text = "  Hello   world  \n\n  Test  "
    assert normalize_text(text) == normalize_text(text)


def test_normalize_empty() -> None:
    assert normalize_text("") == ""
    assert normalize_text("   ") == ""


def test_text_hash_stable() -> None:
    assert text_hash("hello") == text_hash("hello")
    assert text_hash("hello") != text_hash("world")


def test_text_hash_changes_on_text_change() -> None:
    h1 = text_hash("hello world")
    h2 = text_hash("hello world!")
    assert h1 != h2


def test_validate_length_within_limit() -> None:
    ok, count = validate_length("hello")
    assert ok is True
    assert count == 5


def test_validate_length_over_limit() -> None:
    long_text = "x" * 281
    ok, count = validate_length(long_text)
    assert ok is False
    assert count == 281


# -- compose_post pipeline (dispatcher-level) -------------------------------

class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True


@pytest.fixture
def dispatcher(tmp_path: Path) -> Dispatcher:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    from webwire.broker import ReadOnlyBroker
    d._broker = ReadOnlyBroker(sm.sb, d._kill, cfg)  # type: ignore[arg-type]
    return d


async def test_compose_first_invoke_returns_confirmation_required(dispatcher) -> None:
    """The compose pipeline goes through the kernel like other writes."""
    r = await dispatcher.invoke("compose_post", {"text": "Hello from Agent-WebWire!"})
    assert r.ok is True
    policy = r.data["policy"]
    assert policy["verdict"] == "confirmation_required"
    assert "confirmation_token" in r.data["data"]


async def test_compose_dry_run_no_submit(dispatcher) -> None:
    """Phase 4a: execute is a NO-OP (dry_run=True). No browser submit."""
    r1 = await dispatcher.invoke("compose_post", {"text": "Test post"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke("compose_post", {"text": "Test post", "confirmation_token": token})
    policy = r2.data["policy"]
    assert policy["verdict"] == "allow"
    # The execute result should be dry-run.
    exec_data = r2.data.get("data", {})
    assert exec_data.get("dry_run") is True
    assert exec_data.get("note", "").startswith("Phase 4a dry-run")


async def test_compose_token_binds_normalized_text(dispatcher) -> None:
    """Invariant #2: token for text A cannot confirm text B."""
    r1 = await dispatcher.invoke("compose_post", {"text": "First text"})
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke("compose_post", {"text": "Different text", "confirmation_token": token})
    assert r2.ok is False
    assert r2.data["policy"]["blocked_by"] == "intent_mismatch"


async def test_compose_normalized_text_in_preview(dispatcher) -> None:
    """Invariant #1: preview shows normalized text."""
    r = await dispatcher.invoke("compose_post", {"text": "  Hello   world  "})
    preview = r.data["data"].get("preview", "")
    assert "Hello world" in preview  # normalized (stripped + collapsed)


async def test_compose_dedupe_blocks_identical_text(dispatcher) -> None:
    """Invariant #6: same normalized text → same dedupe key → blocked on replay."""
    r1 = await dispatcher.invoke("compose_post", {"text": "Same text"})
    token = r1.data["data"]["confirmation_token"]
    await dispatcher.invoke("compose_post", {"text": "Same text", "confirmation_token": token})
    # Replay same text — dedupe should block.
    r3 = await dispatcher.invoke("compose_post", {"text": "Same text"})
    assert r3.ok is False
    assert r3.data["policy"]["blocked_by"] == "dedupe"


async def test_compose_different_text_not_dedupe_blocked(dispatcher) -> None:
    """Different text → different dedupe key → allowed."""
    r1 = await dispatcher.invoke("compose_post", {"text": "Text one"})
    token = r1.data["data"]["confirmation_token"]
    await dispatcher.invoke("compose_post", {"text": "Text one", "confirmation_token": token})
    # Different text should NOT be dedupe-blocked.
    r2 = await dispatcher.invoke("compose_post", {"text": "Text two"})
    assert r2.ok is True  # confirmation_required, not dedupe-blocked


async def test_compose_over_length_rejected_at_policy(dispatcher) -> None:
    """Text over 280 chars is rejected — no silent truncation."""
    long_text = "x" * 281
    r1 = await dispatcher.invoke("compose_post", {"text": long_text})
    token = r1.data["data"]["confirmation_token"]
    r2 = await dispatcher.invoke("compose_post", {"text": long_text, "confirmation_token": token})
    # Execute should reject (length validation in execute).
    assert r2.data["trace"]["execute_ok"] is False


async def test_compose_kill_switch_blocks(dispatcher) -> None:
    dispatcher.kill_switch.trip()
    r = await dispatcher.invoke("compose_post", {"text": "test"})
    assert r.ok is False
    assert r.failure_category.value == "security"
