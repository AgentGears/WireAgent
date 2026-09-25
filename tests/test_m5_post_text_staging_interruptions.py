"""Regression coverage for clean plain-post staging interruptions."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_post_text_executor import M5PostTextExecutor
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.safety.text_normalize import text_hash


class _InterruptingPostBroker:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.context_owned = False
        self.cleanup_calls = 0
        self.submit_calls = 0
        self.text = ""

    async def fill_composer(self, text: str) -> ActionResult:
        self.context_owned = True
        self.text = text
        if self.mode == "fill_raises":
            raise RuntimeError("fill interrupted after composer binding")
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        if self.mode == "readback_raises":
            raise RuntimeError("readback interrupted")
        return ok_result(data={"composer_text": self.text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError(f"plain post must not attach media: {image_path}")

    async def close_composer(self) -> ActionResult:
        self.cleanup_calls += 1
        self.context_owned = False
        self.text = ""
        return ok_result(data={"cleanup": "closed"})

    async def click_submit(self, **kwargs) -> ActionResult:  # type: ignore[no-untyped-def]
        del kwargs
        self.submit_calls += 1
        raise AssertionError("staging interruption must not reach submit")


class _UnusedEvidence:
    async def capture_pre_submit_ids(self) -> ActionResult:
        raise AssertionError("staging interruption must not reach evidence baseline")

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        del pre_submit_ids, exclude_ids
        raise AssertionError("staging interruption must not reach evidence capture")

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        del post_url, normalized_text
        raise AssertionError("staging interruption must not reach verification")


def _intent() -> WriteIntent:
    text = "hello world"
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
    broker: _InterruptingPostBroker,
) -> tuple[M5PostTextExecutor, EffectLedger]:
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
    return M5PostTextExecutor(runtime=runtime, evidence_reader=_UnusedEvidence()), ledger


async def test_fill_exception_cleans_context_and_terminalizes_no_effect(
    tmp_path: Path,
) -> None:
    broker = _InterruptingPostBroker("fill_raises")
    executor, ledger = _executor(tmp_path, broker)

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert execution.permit_consumed is False
    assert broker.cleanup_calls == 1
    assert broker.context_owned is False
    assert broker.submit_calls == 0
    assert ledger.read_records() == []


async def test_readback_exception_cleans_context_and_terminalizes_no_effect(
    tmp_path: Path,
) -> None:
    broker = _InterruptingPostBroker("readback_raises")
    executor, ledger = _executor(tmp_path, broker)

    execution = await executor.execute(_intent())

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert execution.permit_consumed is False
    assert broker.cleanup_calls == 1
    assert broker.context_owned is False
    assert broker.submit_calls == 0
    assert ledger.read_records() == []
