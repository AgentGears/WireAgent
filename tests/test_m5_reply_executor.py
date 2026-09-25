"""Integration tests for the Layer-5 target-bound reply executor."""

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
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_reply_executor import M5ReplyExecutor
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.safety.text_normalize import text_hash


class _ReplyBroker:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.composer_text = ""
        self.cleanup_calls = 0
        self.submit_calls = 0
        self.gate_calls = 0
        self.open_calls: list[tuple[str, str]] = []

    async def open_reply_on_target(
        self,
        post_url: str,
        target_post_id: str,
    ) -> ActionResult:
        self.open_calls.append((post_url, target_post_id))
        if self.mode == "open_failure":
            return soft_failure("target missing", failure_category=FailureCategory.SECURITY)
        return ok_result(data={"opened": True})

    async def fill_reply_composer(self, text: str) -> ActionResult:
        if self.mode == "fill_failure":
            return soft_failure("fill failed", failure_category=FailureCategory.TIMEOUT)
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        if self.mode == "readback_raise":
            raise RuntimeError("readback interrupted")
        text = "wrong" if self.mode == "text_mismatch" else self.composer_text
        return ok_result(data={"composer_text": text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError(f"plain reply must not attach media: {image_path}")

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
        raise_baseline: bool = False,
        raise_capture: bool = False,
        raise_verify: bool = False,
        reply_post_id: str = "999",
        include_actor: bool = True,
        include_url: bool = True,
    ) -> None:
        self.baseline_ok = baseline_ok
        self.capture_ok = capture_ok
        self.verify_ok = verify_ok
        self.raise_baseline = raise_baseline
        self.raise_capture = raise_capture
        self.raise_verify = raise_verify
        self.reply_post_id = reply_post_id
        self.include_actor = include_actor
        self.include_url = include_url
        self.baseline_calls = 0
        self.capture_calls = 0
        self.verify_calls = 0

    async def capture_pre_submit_ids(self) -> ActionResult:
        self.baseline_calls += 1
        if self.raise_baseline:
            raise RuntimeError("baseline interrupted")
        if not self.baseline_ok:
            return soft_failure("baseline failed", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"status_ids": ["10", "123"]})

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        self.capture_calls += 1
        assert pre_submit_ids == {"10", "123"}
        assert exclude_ids == {"123"}
        if self.raise_capture:
            raise RuntimeError("capture interrupted")
        if not self.capture_ok:
            return soft_failure("capture failed", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "post_id": self.reply_post_id,
                "post_url": f"https://x.com/actor/status/{self.reply_post_id}",
            }
        )

    async def verify_reply_in_thread(
        self,
        *,
        target_post_id: str,
        reply_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult:
        self.verify_calls += 1
        assert target_post_id == "123"
        assert reply_post_id == self.reply_post_id
        assert actor_id == "@actor"
        assert normalized_text == "hello reply"
        if self.raise_verify:
            raise RuntimeError("verification interrupted")
        if not self.verify_ok:
            return soft_failure("thread proof failed", failure_category=FailureCategory.UNKNOWN)
        data = {
            "thread_bound": True,
            "target_post_id": "123",
            "reply_post_id": self.reply_post_id,
            "text_matches": True,
        }
        if self.include_actor:
            data["reply_actor"] = "actor"
        if self.include_url:
            data["reply_url"] = f"https://x.com/actor/status/{self.reply_post_id}"
        return ok_result(data=data)


def _intent(
    text: str = "hello reply",
    *,
    post_url: str = "https://x.com/target/status/123",
) -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require("reply")
    return WriteIntent(
        action_type="reply",
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        semantic_variant=text_hash(text),
        payload={
            "normalized_text": text,
            "char_count": len(text),
            "post_url": post_url,
            "target_post_id": "123",
        },
        actor_identity="@actor",
    )


def _executor(
    tmp_path: Path,
    *,
    broker: _ReplyBroker,
    evidence: _Evidence,
) -> tuple[M5ReplyExecutor, EffectLedger]:
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
    return M5ReplyExecutor(runtime=runtime, evidence_reader=evidence), ledger


async def test_reply_success_records_reserved_then_confirmed(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    evidence = _Evidence()
    executor, ledger = _executor(tmp_path, broker=broker, evidence=evidence)

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert broker.open_calls == [("https://x.com/target/status/123", "123")]
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["target_post_id"] == "123"
    assert terminal.details["reply_post_id"] == "999"
    assert terminal.details["thread_bound"] is True
    assert terminal.details["text_verified"] is True
    assert terminal.details["actor_verified"] is True
    assert terminal.details["url_verified"] is True


async def test_reply_target_id_only_uses_canonical_layer4_url(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, _ = _executor(tmp_path, broker=broker, evidence=_Evidence())

    execution = await executor.execute(_intent(post_url=""))

    assert execution.result.ok is True
    assert broker.open_calls == [("https://x.com/i/status/123", "123")]


async def test_strong_reply_evidence_confirms_uncertain_click(tmp_path: Path) -> None:
    broker = _ReplyBroker(mode="fail_after_gate")
    executor, ledger = _executor(tmp_path, broker=broker, evidence=_Evidence())

    execution = await executor.execute(_intent())

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].details["submit_result_ok"] is False


async def test_reply_capture_failure_after_submit_records_unknown(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(capture_ok=False),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.result.data["reconciliation_required"] is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]


async def test_reply_thread_verification_failure_records_unknown(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(verify_ok=False),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_reply_ok_evidence_without_actor_still_records_unknown(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(include_actor=False),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.EFFECT_UNKNOWN
    assert terminal.details["actor_verified"] is False


async def test_reply_ok_evidence_without_verified_url_still_records_unknown(
    tmp_path: Path,
) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(include_url=False),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.EFFECT_UNKNOWN
    assert terminal.details["url_verified"] is False


async def test_reply_capture_cannot_reuse_target_id(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(reply_post_id="123"),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_reply_capture_interruption_records_unknown(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(raise_capture=True),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["evidence_phase"] == "capture_reply_status"
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_reply_verification_interruption_records_unknown(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(raise_verify=True),
    )

    execution = await executor.execute(_intent())

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["evidence_phase"] == "verify_reply_in_thread"
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_reply_baseline_interruption_is_clean_precommit(tmp_path: Path) -> None:
    broker = _ReplyBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        evidence=_Evidence(raise_baseline=True),
    )

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_reply_readback_interruption_is_clean_precommit(tmp_path: Path) -> None:
    broker = _ReplyBroker(mode="readback_raise")
    executor, ledger = _executor(tmp_path, broker=broker, evidence=_Evidence())

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_reply_target_open_failure_is_clean_precommit(tmp_path: Path) -> None:
    broker = _ReplyBroker(mode="open_failure")
    executor, ledger = _executor(tmp_path, broker=broker, evidence=_Evidence())

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []
