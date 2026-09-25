"""Interruption regressions for post-text evidence before/after commit authority."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
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
    def __init__(self) -> None:
        self.text = ""
        self.cleanup_calls = 0
        self.submit_calls = 0

    async def fill_composer(self, text: str) -> ActionResult:
        self.text = text
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError(f"plain post must not attach media: {image_path}")

    async def close_composer(self) -> ActionResult:
        self.cleanup_calls += 1
        self.text = ""
        return ok_result(data={"closed": True})

    async def click_submit(
        self,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
        _precommit_check,  # type: ignore[no-untyped-def]
        _expected_text: str,
        _expected_attachments: int,
    ) -> ActionResult:
        self.submit_calls += 1
        assert _expected_text == self.text
        assert _expected_attachments == 0
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"submitted": True})


class _RaisingEvidence:
    def __init__(self, *, phase: str) -> None:
        self.phase = phase

    async def capture_pre_submit_ids(self) -> ActionResult:
        if self.phase == "baseline":
            raise RuntimeError("baseline interrupted")
        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        assert pre_submit_ids == {"10", "11"}
        assert exclude_ids in (None, set())
        if self.phase == "capture":
            raise RuntimeError("capture interrupted")
        return ok_result(
            data={
                "post_id": "99",
                "post_url": "https://x.com/actor/status/99",
            }
        )

    async def verify_post_text(
        self,
        post_url: str,
        normalized_text: str,
    ) -> ActionResult:
        assert post_url == "https://x.com/actor/status/99"
        assert normalized_text == "hello world"
        if self.phase == "verify":
            raise RuntimeError("verification interrupted")
        return ok_result(data={"text_matches": True})


def _intent() -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("post")
    text = "hello world"
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
    phase: str,
) -> tuple[M5PostTextExecutor, EffectLedger, _PostBroker]:
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
    broker = _PostBroker()
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
    executor = M5PostTextExecutor(
        runtime=runtime,
        evidence_reader=_RaisingEvidence(phase=phase),
    )
    return executor, ledger, broker


async def test_baseline_exception_is_clean_precommit_no_effect(tmp_path: Path) -> None:
    executor, ledger, broker = _executor(tmp_path, phase="baseline")

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_capture_exception_after_consumption_records_unknown(tmp_path: Path) -> None:
    executor, ledger, broker = _executor(tmp_path, phase="capture")

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["reconciliation_required"] is True
    assert execution.result.data["evidence_phase"] == "capture_new_post"
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert broker.submit_calls == 1
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["phase"] == "capture_new_post"
    assert terminal.details["exception_type"] == "RuntimeError"


async def test_verification_exception_after_consumption_records_unknown(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(tmp_path, phase="verify")

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["reconciliation_required"] is True
    assert execution.result.data["evidence_phase"] == "verify_post_text"
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["phase"] == "verify_post_text"
    assert terminal.details["exception_type"] == "RuntimeError"
    assert terminal.details["posted_post_id"] == "99"
    assert terminal.details["posted_url"] == "https://x.com/actor/status/99"
