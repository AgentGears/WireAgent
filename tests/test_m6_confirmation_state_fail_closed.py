"""Fail-closed integration checks for M6 confirmation-state faults."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    ConfirmationState,
    DedupeStore,
    KillSwitch,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _LikeCapability:
    name = "like"

    def __init__(self) -> None:
        self.execute_calls = 0

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.require("like")
        return WriteIntent(
            action_type="like",
            target_type="post",
            target_id=str(input.get("post_id", "1")),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del intent, broker
        return PreviewResult(summary="preview")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        self.execute_calls += 1
        return ok_result(data={"executed": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _kernel(tmp_path: Path, state: ConfirmationState) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        confirmation_state=state,
    )


async def test_regressing_authority_clock_becomes_security_denial_before_execute(
    tmp_path: Path,
) -> None:
    monotonic = _Clock(10.0)
    state = ConfirmationState(
        monotonic_clock=monotonic,
        wall_clock=_Clock(1_000.0),
        token_factory=lambda: "token-1",
    )
    kernel = _kernel(tmp_path, state)
    cap = _LikeCapability()

    first = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "1"},
        actor_identity="alice",
    )
    token = first.data["data"]["confirmation_token"]

    monotonic.value = 9.0
    second = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "1", "confirmation_token": token},
        actor_identity="alice",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "confirmation_state_unavailable"
    assert "denied:confirmation_state_unavailable" in second.data["trace"]["stages"]
    assert cap.execute_calls == 0
