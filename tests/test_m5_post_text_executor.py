"""Integration tests for the Layer-5 post_text executor."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_post_text_executor import M5PostTextExecutor
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.safety.text_normalize import text_hash


class _PostBroker:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.composer_text = ""
        self.cleanup_calls = 0
        self.submit_calls = 0
        self.gate_calls = 0

    async def fill_composer(self, text: str) -> ActionResult:
        if self.mode == "fill_failure":
            return soft_failure("fill failed", failure_category=FailureCategory.TIMEOUT)
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        text = "wrong" if self.mode == "text_mismatch" else self.composer_text
        return ok_result(data={"composer_text": text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError(f"plain post must not attach media: {image_path}")

    async def close_composer(self) -> ActionResult:
        self.cleanup_calls += 1
        self.composer_text = ""
        return ok_result(data={"cleanup": "closed"})

    async def click_submit(
        self,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
        _precommit_check,  # type: ignore[no-untyped-def]
        _expected_text: str,
        _expected_attachments: int,
    ) -> ActionResult:
        self.submit_calls += 1
        assert _expected_text == self.composer_text
        assert _expected_attachments == 0
        checked = await _precommit_check()
        if checked is not None:
            return checked
        if self.mode == "fail_before_gate":
            return soft_failure(
                "submit control missing before gate",
                failure_category=FailureCategory.SECURITY,
            )
        self.gate_calls += 1
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.mode == "raise_after_gate":
            raise RuntimeError("submit interrupted")
        if self.mode == "fail_after_gate":
            return soft_failure(
                "bound click uncertain",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(data={"submitted": True})


class _Evidence:
    def __init__(
        self,
        *,
        baseline_ok: bool = True,
        capture_ok: bool = True,
        verify_ok: bool = True,
    ) -> None:
        self.baseline_ok = baseline_ok
        self.capture_ok = capture_ok
        self.verify_ok = verify_ok
        self.baseline_calls = 0
        self.capture_calls = 0
        self.verify_calls = 0
        self.approved_text: str | None = None

    async def capture_pre_submit_ids(self) -> ActionResult:
        self.baseline_calls += 1
        if not self.baseline_ok:
            return soft_failure("baseline failed", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        self.capture_calls += 1
        assert pre_submit_ids == {"10", "11"}
        assert exclude_ids in (None, set())
        if not self.capture_ok:
            return soft_failure("capture failed", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "post_id": "99",
                "post_url": "https://x.com/actor/status/99",
            }
        )

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        self.verify_calls += 1
        self.approved_text = normalized_text
        assert post_url == "https://x.com/actor/status/99"
        if not self.verify_ok:
            return soft_failure("text mismatch", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"text_matches": True})


class _ReservationFailLedger(EffectLedger):
    def append_durable(self, record: EffectLedgerRecord) -> None:
        if record.state is EffectState.RESERVED:
            raise EffectLedgerError("reservation durability failed")
        return super().append_durable(record)


class _KillBeforeConsumeGateway(CommitGateway):
    def __init__(self, *, kill_switch: KillSwitch, **kwargs: Any) -> None:
        super().__init__(kill_switch=kill_switch, **kwargs)
        self._test_kill = kill_switch
        self._trip_once = True

    def consume_permit(self, permit, **kwargs):  # type: ignore[no-untyped-def]
        if self._trip_once:
            self._trip_once = False
            self._test_kill.trip()
        return super().consume_permit(permit, **kwargs)


def _intent(text: str = "hello world") -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    return WriteIntent(
        action_type="post",
        target_type="none",
        target_id="none",
        risk_meta=risk,
        compensation=compensation,
        semantic_variant=text_hash(text),
        payload={"normalized_text": text, "char_count": len(text)},
        actor_identity="@actor",
    )


def _executor(
    tmp_path: Path,
    *,
    broker: _PostBroker,
    evidence: _Evidence,
    ledger: EffectLedger | None = None,
    kill_before_consume: bool = False,
) -> tuple[M5PostTextExecutor, EffectLedger, KillSwitch]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    active_ledger = ledger or EffectLedger(cfg)
    gateway_cls = _KillBeforeConsumeGateway if kill_before_consume else CommitGateway
    gateway = gateway_cls(
        ledger=active_ledger,
        kill_switch=kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(
        broker,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    return (
        M5PostTextExecutor(runtime=runtime, evidence_reader=evidence),
        active_ledger,
        kill,
    )


async def test_post_text_success_records_reserved_then_confirmed(tmp_path: Path) -> None:
    broker = _PostBroker()
    evidence = _Evidence()
    executor, ledger, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert evidence.baseline_calls == 1
    assert evidence.capture_calls == 1
    assert evidence.verify_calls == 1
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["posted_post_id"] == "99"
    assert terminal.details["text_verified"] is True


async def test_strong_post_evidence_confirms_even_if_click_result_was_uncertain(
    tmp_path: Path,
) -> None:
    broker = _PostBroker(mode="fail_after_gate")
    evidence = _Evidence()
    executor, ledger, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.EFFECT_CONFIRMED
    assert terminal.details["submit_result_ok"] is False
    assert terminal.details["text_verified"] is True


async def test_capture_failure_after_submit_records_unknown(tmp_path: Path) -> None:
    broker = _PostBroker()
    evidence = _Evidence(capture_ok=False)
    executor, ledger, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["reconciliation_required"] is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]


async def test_text_verification_failure_after_submit_records_unknown(
    tmp_path: Path,
) -> None:
    broker = _PostBroker()
    evidence = _Evidence(verify_ok=False)
    executor, ledger, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_baseline_failure_is_clean_precommit_no_effect(tmp_path: Path) -> None:
    broker = _PostBroker()
    evidence = _Evidence(baseline_ok=False)
    executor, ledger, _ = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_fill_failure_is_clean_precommit_no_effect(tmp_path: Path) -> None:
    broker = _PostBroker(mode="fill_failure")
    executor, ledger, _ = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_reservation_failure_keeps_attempt_unresolved_but_cleans_composer(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    ledger = _ReservationFailLedger(cfg)
    broker = _PostBroker()
    executor, _, _ = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(),
        ledger=ledger,
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.PREPARING
    assert execution.permit_issued is False
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_kill_denied_consumption_closes_reserved_no_effect_and_cleans(
    tmp_path: Path,
) -> None:
    broker = _PostBroker()
    executor, ledger, kill = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(),
        kill_before_consume=True,
    )

    execution = await executor.execute(_intent())

    assert kill.tripped() is True
    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is True
    assert execution.permit_consumed is False
    assert broker.cleanup_calls == 1
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]


async def test_exception_after_submit_authority_records_unknown_without_reraise(
    tmp_path: Path,
) -> None:
    broker = _PostBroker(mode="raise_after_gate")
    executor, ledger, _ = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["public_side_effect"] is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    assert ledger.read_records()[-1].details["phase"] == "submit"
