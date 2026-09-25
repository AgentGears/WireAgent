"""Integration tests for the Layer-5 target-bound delete executor."""

from __future__ import annotations

from pathlib import Path

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_delete_executor import M5DeleteExecutor
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker


class _DeleteBroker:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.calls: list[tuple[str, str]] = []
        self.gate_calls = 0

    async def delete_post(
        self,
        post_url: str,
        post_id: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        self.calls.append((post_url, post_id))
        if self.mode == "fail_before_gate":
            return soft_failure("staging failed", failure_category=FailureCategory.SECURITY)
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.mode == "raise_after_gate":
            raise RuntimeError("delete interrupted")
        if self.mode == "uncertain_after_gate":
            return soft_failure("confirm click uncertain", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"deleted": True, "post_id": post_id})


class _Evidence:
    def __init__(self, states: list[str], *, raise_call: int | None = None) -> None:
        self.states = list(states)
        self.raise_call = raise_call
        self.calls: list[tuple[str, str]] = []

    async def read_delete_state(self, post_url: str, post_id: str) -> ActionResult:
        self.calls.append((post_url, post_id))
        if self.raise_call == len(self.calls):
            raise RuntimeError("evidence interrupted")
        state = self.states.pop(0)
        if state == "unknown":
            return soft_failure("unknown delete state", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "post_state": state,
                "target_post_id": post_id,
                "evidence": (
                    "explicit_delete_tombstone"
                    if state == "deleted"
                    else "unique_direct_target_article"
                ),
            }
        )


def _intent(*, post_url: str = "https://x.com/actor/status/123") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("delete_post")
    return WriteIntent(
        action_type="delete_post",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        actor_identity="@actor",
        payload={"post_url": post_url, "target_post_id": "123"},
    )


def _executor(
    tmp_path: Path,
    *,
    broker: _DeleteBroker,
    evidence: _Evidence,
) -> tuple[M5DeleteExecutor, EffectLedger]:
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
    scoped = ScopedAuthorityBroker(broker, gateway, policies=DEFAULT_EFFECT_POLICIES)
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    return M5DeleteExecutor(runtime=runtime, evidence_reader=evidence), ledger


async def test_delete_success_records_reserved_then_confirmed(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    evidence = _Evidence(["present", "deleted"])
    executor, ledger = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert broker.gate_calls == 1
    assert [row.state for row in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["baseline_state"] == "present"
    assert terminal.details["verified_state"] == "deleted"
    assert terminal.details["verification_evidence"] == "explicit_delete_tombstone"


async def test_strong_delete_evidence_confirms_uncertain_confirm_click(tmp_path: Path) -> None:
    broker = _DeleteBroker(mode="uncertain_after_gate")
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["present", "deleted"]),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].details["mutation_ok"] is False


async def test_delete_still_present_after_consumption_records_unknown(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["present", "present"]),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["reconciliation_required"] is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_delete_verification_interruption_records_unknown(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["present", "deleted"], raise_call=2),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["reconciliation_required"] is True
    assert ledger.read_records()[-1].details["phase"] == "delete_verification"


async def test_delete_mutation_interruption_after_gate_records_unknown(tmp_path: Path) -> None:
    broker = _DeleteBroker(mode="raise_after_gate")
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["present"]),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["reconciliation_required"] is True
    assert ledger.read_records()[-1].details["phase"] == "delete_mutation"


async def test_already_deleted_baseline_is_clean_no_effect(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["deleted"]),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.calls == []
    assert ledger.read_records() == []


async def test_unknown_baseline_is_clean_no_effect(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["unknown"]),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.calls == []
    assert ledger.read_records() == []


async def test_delete_failure_before_commit_gate_is_clean_no_effect(tmp_path: Path) -> None:
    broker = _DeleteBroker(mode="fail_before_gate")
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(["present"]),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.gate_calls == 0
    assert ledger.read_records() == []


async def test_delete_target_id_only_uses_canonical_layer4_url(tmp_path: Path) -> None:
    broker = _DeleteBroker()
    evidence = _Evidence(["present", "deleted"])
    executor, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent(post_url=""))

    assert execution.result.ok is True
    expected = "https://x.com/i/status/123"
    assert evidence.calls == [(expected, "123"), (expected, "123")]
    assert broker.calls == [(expected, "123")]
