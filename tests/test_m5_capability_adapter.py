"""WriteKernel integration tests for the Layer-5 engagement adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.journal import Journal
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.dedupe import DedupeStore
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.models import PolicyVerdict, WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.write_kernel import PreviewResult, WriteKernel


class _M5Broker:
    def __init__(self, *, fail_after_gate: bool = False) -> None:
        self.fail_after_gate = fail_after_gate
        self.gate_calls = 0

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.fail_after_gate:
            return soft_failure(
                "bookmark click uncertain",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(data={"bookmarked": True, "post_url": post_url})

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.fail_after_gate:
            return soft_failure(
                "like click uncertain",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(data={"liked": True, "post_url": post_url})


class _EvidenceReader:
    def __init__(
        self,
        *,
        bookmark_state: str = "bookmarked",
        like_state: str = "liked",
    ) -> None:
        self.bookmark_state = bookmark_state
        self.like_state = like_state

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        return ok_result(data={"bookmark_state": self.bookmark_state, "url": post_url})

    async def read_like_state(self, post_url: str) -> ActionResult:
        return ok_result(data={"like_state": self.like_state, "url": post_url})


class _OriginalCapability:
    def __init__(self, *, action: str) -> None:
        self.action = action
        self.name = f"{action}_post"
        self.execute_calls = 0
        self.verify_calls = 0

    @property
    def tier(self) -> CapabilityTier:
        return CapabilityTier.WRITE

    def compose(
        self,
        input: dict[str, Any],
        actor_identity: Optional[str],
    ) -> WriteIntent:
        risk, compensation = DEFAULT_REGISTRY.require(self.action)
        post_id = str(input.get("post_id", "123"))
        return WriteIntent(
            action_type=self.action,
            target_type="post",
            target_id=post_id,
            risk_meta=risk,
            compensation=compensation,
            payload={"post_url": f"https://x.com/u/status/{post_id}"},
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del broker
        return PreviewResult(
            summary=f"Will {self.action} post {intent.target_id}",
            target_url=f"https://x.com/u/status/{intent.target_id}",
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.execute_calls += 1
        raise AssertionError("original capability must not receive legacy write broker")

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.verify_calls += 1
        raise AssertionError("original capability must not receive legacy verify broker")


class _LegacyWriteBrokerSentinel:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"legacy write broker must not be used: {name}")


class _ReadBroker:
    pass


def _stack(
    tmp_path: Path,
    *,
    action: str,
    fail_after_gate: bool = False,
    bookmark_state: str = "bookmarked",
    like_state: str = "liked",
) -> tuple[
    WriteKernel,
    M5EngagementCapabilityAdapter,
    _OriginalCapability,
    EffectLedger,
    DedupeStore,
]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    ledger = EffectLedger(cfg)
    gateway = CommitGateway(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    m5_broker = _M5Broker(fail_after_gate=fail_after_gate)
    scoped = ScopedAuthorityBroker(
        m5_broker,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    executor = M5EffectExecutor(
        runtime=runtime,
        evidence_reader=_EvidenceReader(
            bookmark_state=bookmark_state,
            like_state=like_state,
        ),
    )
    capability = _OriginalCapability(action=action)
    adapter = M5EngagementCapabilityAdapter(capability, executor)
    dedupe = DedupeStore(ttl_seconds=3600)
    kernel = WriteKernel(
        kill_switch=kill,
        risk_registry=DEFAULT_REGISTRY,
        token_bucket=TokenBucket(),
        dedupe=dedupe,
        journal=Journal(cfg),
        write_broker_factory=lambda: _LegacyWriteBrokerSentinel(),
    )
    return kernel, adapter, capability, ledger, dedupe


async def _confirm_and_execute(
    kernel: WriteKernel,
    adapter: M5EngagementCapabilityAdapter,
) -> ActionResult:
    read_broker = _ReadBroker()
    first = await kernel.execute(
        adapter,
        read_broker,  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="@actor",
    )
    assert first.data["policy"]["verdict"] == PolicyVerdict.CONFIRMATION_REQUIRED.value
    token = first.data["data"]["confirmation_token"]
    return await kernel.execute(
        adapter,
        read_broker,  # type: ignore[arg-type]
        {"post_id": "123", "confirmation_token": token},
        actor_identity="@actor",
    )


async def test_bookmark_canary_bypasses_original_legacy_mutation_methods(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, ledger, _ = _stack(tmp_path, action="bookmark")

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert result.data["trace"]["execute_ok"] is True
    assert result.data["trace"]["verify_ok"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert [record.state for record in ledger.read_records()] == [
        EffectState.EFFECT_CONFIRMED
    ]


async def test_like_canary_preserves_required_reservation_and_confirmation(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, ledger, _ = _stack(tmp_path, action="like")

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == PolicyVerdict.ALLOW.value
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]


async def test_unknown_post_authority_failure_marks_public_side_effect_for_dedupe(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, ledger, dedupe = _stack(
        tmp_path,
        action="like",
        fail_after_gate=True,
    )

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == PolicyVerdict.DENY.value
    assert result.data["trace"]["execute_ok"] is False
    assert result.data["trace"]["verify_ok"] is False
    assert result.data["trace"]["dedupe_recorded"] is True
    assert result.data["data"]["reconciliation_required"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert adapter._execution.get() is None
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    intent = capability.compose({"post_id": "123"}, "@actor")
    assert dedupe.check(intent.dedupe_key()) is False


async def test_successful_click_with_unknown_readback_is_denied_and_deduped(
    tmp_path: Path,
) -> None:
    kernel, adapter, capability, ledger, dedupe = _stack(
        tmp_path,
        action="like",
        like_state="unknown",
    )

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == PolicyVerdict.DENY.value
    assert result.data["trace"]["execute_ok"] is False
    assert result.data["trace"]["verify_ok"] is False
    assert result.data["trace"]["dedupe_recorded"] is True
    assert result.data["data"]["m5_effect_state"] == AttemptState.EFFECT_UNKNOWN.value
    assert result.data["data"]["public_side_effect"] is True
    assert result.data["data"]["reconciliation_required"] is True
    assert adapter._execution.get() is None
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    intent = capability.compose({"post_id": "123"}, "@actor")
    assert dedupe.check(intent.dedupe_key()) is False
