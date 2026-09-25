"""Regression for per-invocation M5 engagement receipt cleanup."""

from __future__ import annotations

from typing import Any

from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter


class _Capability:
    name = "like_post"


class _RaisingExecutor:
    async def execute(self, intent: Any):  # type: ignore[no-untyped-def]
        del intent
        raise RuntimeError("executor exploded")


async def test_execute_exception_clears_stale_receipt_before_executor_call() -> None:
    adapter = M5EngagementCapabilityAdapter(
        _Capability(),
        _RaisingExecutor(),  # type: ignore[arg-type]
    )
    adapter._execution.set(object())  # type: ignore[arg-type]

    try:
        await adapter.execute(object(), object())  # type: ignore[arg-type]
    except RuntimeError as exc:
        assert str(exc) == "executor exploded"
    else:  # pragma: no cover - fail loudly if the executor stops raising
        raise AssertionError("raising executor unexpectedly returned")

    assert adapter._execution.get() is None
