"""WriteKernel integration tests for the Layer-5 ``reply_post`` adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.capabilities.reply_post import ReplyPostCapability
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.execution_models import AttemptState
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_reply_adapter import M5ReplyCapabilityAdapter
from webwire.safety.m5_reply_executor import M5ReplyExecution
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.write_kernel import WriteKernel


class _OriginalReplyCapability(ReplyPostCapability):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.verify_calls = 0

    async def execute(self, intent, broker):  # type: ignore[no-untyped-def]
        self.execute_calls += 1
        raise AssertionError("original reply_post execute must not receive legacy broker")

    async def verify(self, intent, broker):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        raise AssertionError("original reply_post verify must not receive legacy broker")


class _FakeReplyExecutor:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls = 0

    async def execute(self, intent) -> M5ReplyExecution:  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.unknown:
            result = hard_failure(
                "reply outcome unknown",
                failure_category=FailureCategory.UNKNOWN,
            )
            result.data = {
                "public_side_effect": True,
                "reconciliation_required": True,
                "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            }
            return M5ReplyExecution(
                result=result,
                verification=None,
                attempt_state=AttemptState.EFFECT_UNKNOWN,
                permit_issued=True,
                permit_consumed=True,
            )
        verification = ok_result(
            data={
                "thread_bound": True,
                "target_post_id": "123",
                "reply_post_id": "999",
                "text_matches": True,
            }
        )
        return M5ReplyExecution(
            result=ok_result(
                data={
                    "result": "reply_posted_and_target_verified",
                    "posted_url": "https://x.com/actor/status/999",
                    "posted_post_id": "999",
                    "target_post_id": "123",
                    "submitted_text": intent.payload["normalized_text"],
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            ),
            verification=verification,
            attempt_state=AttemptState.EFFECT_CONFIRMED,
            permit_issued=True,
            permit_consumed=True,
        )


class _LegacyBrokerSentinel:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"legacy write broker must not be used: {name}")


class _ReadBroker:
    pass


def _kernel(
    tmp_path: Path,
    *,
    unknown: bool = False,
) -> tuple[
    WriteKernel,
    M5ReplyCapabilityAdapter,
    _OriginalReplyCapability,
    _FakeReplyExecutor,
    DedupeStore,
]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    capability = _OriginalReplyCapability()
    executor = _FakeReplyExecutor(unknown=unknown)
    adapter = M5ReplyCapabilityAdapter(capability, executor)  # type: ignore[arg-type]
    dedupe = DedupeStore(ttl_seconds=3600)
    kernel = WriteKernel(
        kill_switch=KillSwitch(cfg),
        risk_registry=DEFAULT_REGISTRY,
        token_bucket=TokenBucket(),
        dedupe=dedupe,
        journal=Journal(cfg),
        write_broker_factory=lambda: _LegacyBrokerSentinel(),
    )
    return kernel, adapter, capability, executor, dedupe


async def _confirm_and_execute(
    kernel: WriteKernel,
    adapter: M5ReplyCapabilityAdapter,
) -> ActionResult:
    payload = {
        "post_url": "https://x.com/target/status/123",
        "target_post_id": "123",
        "text": "approved reply",
    }
    first = await kernel.execute(
        adapter,
        _ReadBroker(),  # type: ignore[arg-type]
        payload,
        actor_identity="@actor",
    )
    assert first.data["policy"]["verdict"] == "confirmation_required"
    token = first.data["data"]["confirmation_token"]
    return await kernel.execute(
        adapter,
        _ReadBroker(),  # type: ignore[arg-type]
        {**payload, "confirmation_token": token},
        actor_identity="@actor",
    )


async def test_reply_adapter_bypasses_original_legacy_methods(tmp_path: Path) -> None:
    kernel, adapter, capability, executor, _ = _kernel(tmp_path)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "allow"
    assert result.data["trace"]["execute_ok"] is True
    assert result.data["trace"]["verify_ok"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert executor.calls == 1


async def test_reply_unknown_marks_dedupe_and_never_calls_legacy_methods(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, executor, dedupe = _kernel(tmp_path, unknown=True)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "deny"
    assert result.data["trace"]["execute_ok"] is False
    assert result.data["trace"]["verify_ok"] is False
    assert result.data["trace"]["dedupe_recorded"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert executor.calls == 1
    intent = capability.compose(
        {
            "post_url": "https://x.com/target/status/123",
            "target_post_id": "123",
            "text": "approved reply",
        },
        "@actor",
    )
    assert dedupe.check(intent.dedupe_key()) is False
