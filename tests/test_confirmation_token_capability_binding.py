"""Regression for confirmation-token authority across equal-intent capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.capabilities.compose_post import ComposePostCapability
from webwire.capabilities.post_text import PostTextCapability
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    KillSwitch,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


class _EqualIntentCapability:
    """Two capability names deliberately manufacture the same WriteIntent."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.execute_calls = 0

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.require("post")
        return WriteIntent(
            action_type="post",
            target_type="none",
            target_id="none",
            risk_meta=meta,
            compensation=comp,
            semantic_variant="same-text-hash",
            actor_identity=actor_identity,
            payload={"normalized_text": str(input.get("text", "")), "char_count": 5},
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del broker
        return PreviewResult(summary=f"preview {intent.action_type}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        self.execute_calls += 1
        return ok_result(data={"executed": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _kernel(tmp_path: Path) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
    )


async def test_confirmation_token_cannot_cross_equal_intent_capabilities(
    tmp_path: Path,
) -> None:
    kernel = _kernel(tmp_path)
    compose_shell = _EqualIntentCapability("compose_post")
    live_post = _EqualIntentCapability("post_text")
    broker = object()

    shell_intent = compose_shell.compose({"text": "hello"}, "alice")
    live_intent = live_post.compose({"text": "hello"}, "alice")
    assert shell_intent.intent_hash() == live_intent.intent_hash()

    first = await kernel.execute(
        compose_shell,
        broker,  # type: ignore[arg-type]
        {"text": "hello"},
        actor_identity="alice",
    )
    token = first.data["data"]["confirmation_token"]

    second = await kernel.execute(
        live_post,
        broker,  # type: ignore[arg-type]
        {"text": "hello", "confirmation_token": token},
        actor_identity="alice",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "capability_mismatch"
    assert live_post.execute_calls == 0


async def test_compose_post_token_cannot_authorize_post_text(tmp_path: Path) -> None:
    kernel = _kernel(tmp_path)
    compose_shell = ComposePostCapability()
    live_post = PostTextCapability()
    broker = object()
    payload = {"text": "hello"}

    shell_intent = compose_shell.compose(payload, "alice")
    live_intent = live_post.compose(payload, "alice")
    assert shell_intent.intent_hash() == live_intent.intent_hash()

    first = await kernel.execute(
        compose_shell,
        broker,  # type: ignore[arg-type]
        payload,
        actor_identity="alice",
    )
    token = first.data["data"]["confirmation_token"]

    second = await kernel.execute(
        live_post,
        broker,  # type: ignore[arg-type]
        {"text": "hello", "confirmation_token": token},
        actor_identity="alice",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "capability_mismatch"
