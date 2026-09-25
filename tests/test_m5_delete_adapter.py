"""WriteKernel integration tests for the Layer-5 delete adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.capabilities.delete_post import DeletePostCapability
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.execution_models import AttemptState
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_delete_adapter import M5DeleteCapabilityAdapter
from webwire.safety.m5_delete_executor import M5DeleteExecution
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.write_kernel import WriteKernel


class _OriginalDeleteCapability(DeletePostCapability):
    def __init__(self) -> None:
        self.execute_calls = 0
        self.verify_calls = 0

    async def execute(self, intent, broker):  # type: ignore[no-untyped-def]
        self.execute_calls += 1
        raise AssertionError("original delete execute must not receive legacy broker")

    async def verify(self, intent, broker):  # type: ignore[no-untyped-def]
        self.verify_calls += 1
        raise AssertionError("original delete verify must not receive legacy broker")


class _FakeDeleteExecutor:
    def __init__(self, *, unknown: bool = False, unknown_ok: bool = False) -> None:
        self.unknown = unknown
        self.unknown_ok = unknown_ok
        self.calls = 0

    async def execute(self, intent) -> M5DeleteExecution:  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.unknown_ok:
            return M5DeleteExecution(
                result=ok_result(
                    data={
                        "result": "delete_submit_returned_ok_but_outcome_unknown",
                        "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
                    }
                ),
                verification=None,
                attempt_state=AttemptState.EFFECT_UNKNOWN,
                permit_issued=True,
                permit_consumed=True,
            )
        if self.unknown:
            result = hard_failure("delete outcome unknown", failure_category=FailureCategory.UNKNOWN)
            result.data = {
                "public_side_effect": True,
                "reconciliation_required": True,
                "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            }
            return M5DeleteExecution(
                result=result,
                verification=None,
                attempt_state=AttemptState.EFFECT_UNKNOWN,
                permit_issued=True,
                permit_consumed=True,
            )
        verification = ok_result(data={"post_state": "deleted"})
        return M5DeleteExecution(
            result=ok_result(
                data={
                    "result": "post_deleted_and_verified",
                    "target_post_id": intent.target_id,
                    "post_state": "deleted",
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
    async def navigate(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return ok_result(data={"url": args[0] if args else None})

    async def probe_selectors(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        return ok_result(data={"probes": {"target_article": True}})


def _kernel(
    tmp_path: Path,
    *,
    unknown: bool = False,
    unknown_ok: bool = False,
) -> tuple[
    WriteKernel,
    M5DeleteCapabilityAdapter,
    _OriginalDeleteCapability,
    _FakeDeleteExecutor,
    DedupeStore,
]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    capability = _OriginalDeleteCapability()
    executor = _FakeDeleteExecutor(unknown=unknown, unknown_ok=unknown_ok)
    adapter = M5DeleteCapabilityAdapter(capability, executor)  # type: ignore[arg-type]
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
    adapter: M5DeleteCapabilityAdapter,
) -> ActionResult:
    payload = {"post_url": "https://x.com/actor/status/123", "target_post_id": "123"}
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


async def test_delete_adapter_bypasses_original_legacy_methods(tmp_path: Path) -> None:
    kernel, adapter, capability, executor, _ = _kernel(tmp_path)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "allow"
    assert result.data["trace"]["execute_ok"] is True
    assert result.data["trace"]["verify_ok"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert executor.calls == 1


async def test_delete_unknown_marks_dedupe_and_never_calls_legacy_methods(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, executor, dedupe = _kernel(tmp_path, unknown=True)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "deny"
    assert result.data["trace"]["execute_ok"] is False
    assert result.data["trace"]["dedupe_recorded"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert executor.calls == 1
    intent = capability.compose(
        {"post_url": "https://x.com/actor/status/123", "target_post_id": "123"},
        "@actor",
    )
    assert dedupe.check(intent.dedupe_key()) is False


async def test_delete_adapter_fails_closed_if_unknown_executor_drifts_to_ok(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, executor, dedupe = _kernel(tmp_path, unknown_ok=True)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "deny"
    assert result.data["trace"]["execute_ok"] is False
    assert result.data["trace"]["verify_ok"] is False
    assert result.data["trace"]["dedupe_recorded"] is True
    assert result.data["data"]["public_side_effect"] is True
    assert result.data["data"]["reconciliation_required"] is True
    assert result.data["data"]["m5_effect_state"] == AttemptState.EFFECT_UNKNOWN.value
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert executor.calls == 1
    intent = capability.compose(
        {"post_url": "https://x.com/actor/status/123", "target_post_id": "123"},
        "@actor",
    )
    assert dedupe.check(intent.dedupe_key()) is False


async def test_delete_adapter_verify_unknown_is_reconciliation_failure(
    tmp_path: Path,
) -> None:
    _, adapter, capability, _, _ = _kernel(tmp_path)
    intent = capability.compose(
        {"post_url": "https://x.com/actor/status/123", "target_post_id": "123"},
        "@actor",
    )
    adapter._execution.set(  # noqa: SLF001 - targeted transitional-contract regression
        M5DeleteExecution(
            result=ok_result(data={"m5_effect_state": AttemptState.EFFECT_UNKNOWN.value}),
            verification=None,
            attempt_state=AttemptState.EFFECT_UNKNOWN,
            permit_issued=True,
            permit_consumed=True,
        )
    )

    result = await adapter.verify(intent, _LegacyBrokerSentinel())

    assert result.ok is False
    assert result.failure_category is FailureCategory.UNKNOWN
    assert result.data["public_side_effect"] is True
    assert result.data["reconciliation_required"] is True
    assert result.data["m5_effect_state"] == AttemptState.EFFECT_UNKNOWN.value
