"""Integration tests for the generic Layer-5 scoped media executor."""

from __future__ import annotations

from pathlib import Path

from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.attachment import file_sha256
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.m5_media_executor import M5MediaExecutor
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker
from webwire.safety.text_normalize import text_hash


class _MediaBroker:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.composer_text = ""
        self.attachments: list[str] = []
        self.events: list[str] = []
        self.cleanup_calls = 0
        self.submit_calls = 0

    async def fill_composer(self, text: str) -> ActionResult:
        self.events.append("fill_post")
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        self.events.append(f"open_reply:{target_post_id}:{post_url}")
        return ok_result(data={"opened": True})

    async def fill_reply_composer(self, text: str) -> ActionResult:
        self.events.append("fill_reply")
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        self.events.append(f"open_quote:{target_post_id}:{post_url}")
        return ok_result(data={"opened": True})

    async def fill_quote_composer(self, text: str) -> ActionResult:
        self.events.append("fill_quote")
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def attach_media(self, image_path: str) -> ActionResult:
        self.events.append(f"attach:{Path(image_path).name}")
        self.attachments.append(image_path)
        return ok_result(data={"attached": True})

    async def verify_attachment_ready(self) -> ActionResult:
        self.events.append("ready")
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        self.events.append("count")
        count = len(self.attachments)
        if self.mode == "precommit_count_mismatch" and count == 1:
            count = 0
        return ok_result(data={"count": count})

    async def read_composer_text(self) -> ActionResult:
        self.events.append("readback")
        return ok_result(data={"composer_text": self.composer_text})

    async def close_composer(self) -> ActionResult:
        self.cleanup_calls += 1
        self.events.append("cleanup")
        self.composer_text = ""
        self.attachments.clear()
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
        self.events.append("submit")
        assert _expected_text == self.composer_text
        assert _expected_attachments == len(self.attachments)
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        if self.mode == "raise_after_gate":
            raise RuntimeError("submit interrupted")
        if self.mode == "uncertain_after_gate":
            return soft_failure("click uncertain", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"submitted": True})


class _ContentEvidence:
    def __init__(self, *, strong: bool = True) -> None:
        self.strong = strong
        self.capture_exclude: set[str] | None = None

    async def capture_pre_submit_ids(self) -> ActionResult:
        return ok_result(data={"status_ids": ["10", "123"]})

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        assert pre_submit_ids == {"10", "123"}
        self.capture_exclude = set(exclude_ids or ())
        return ok_result(
            data={
                "post_id": "999",
                "post_url": "https://x.com/actor/status/999",
            }
        )

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        assert post_url == "https://x.com/actor/status/999"
        if not self.strong:
            return soft_failure("text proof missing", failure_category=FailureCategory.UNKNOWN)
        return ok_result(data={"text_matches": True, "post_url": post_url})

    async def verify_reply_in_thread(
        self,
        *,
        target_post_id: str,
        reply_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult:
        assert target_post_id == "123"
        assert reply_post_id == "999"
        assert actor_id == "@actor"
        if not self.strong:
            return soft_failure("reply proof missing", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "thread_bound": True,
                "target_post_id": "123",
                "reply_post_id": "999",
                "reply_actor": "actor",
                "reply_url": "https://x.com/actor/status/999",
                "text_matches": True,
            }
        )

    async def verify_quote_attachment(
        self,
        *,
        quote_post_id: str,
        target_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult:
        assert quote_post_id == "999"
        assert target_post_id == "123"
        assert actor_id == "@actor"
        if not self.strong:
            return soft_failure("quote proof missing", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "quote_attachment_verified": True,
                "target_post_id": "123",
                "quote_post_id": "999",
                "quote_actor": "actor",
                "quote_url": "https://x.com/actor/status/999",
                "text_matches": True,
            }
        )


class _MediaEvidence:
    def __init__(self, *, count: int) -> None:
        self.count = count
        self.urls: list[str] = []

    async def count_post_media(self, post_url: str) -> ActionResult:
        self.urls.append(post_url)
        if self.count <= 0:
            return soft_failure("media missing", failure_category=FailureCategory.UNKNOWN)
        return ok_result(
            data={
                "post_url": post_url,
                "media_count": self.count,
                "source_byte_equivalence_verified": False,
            }
        )


def _intent(
    tmp_path: Path,
    *,
    action_type: str = "post",
    count: int = 2,
    text: str = "approved media",
    post_url: str = "https://x.com/target/status/123",
) -> tuple[WriteIntent, list[Path]]:
    paths: list[Path] = []
    items: list[dict[str, object]] = []
    for index in range(count):
        path = tmp_path / f"image-{index}.jpg"
        path.write_bytes(f"image-{index}".encode())
        paths.append(path)
        items.append(
            {
                "index": index,
                "source_path": str(path),
                "sha256": file_sha256(path),
            }
        )
    risk, compensation = DEFAULT_REGISTRY.require(action_type)
    target_type = "none" if action_type == "post" else "post"
    target_id = "none" if action_type == "post" else "123"
    payload: dict[str, object] = {
        "normalized_text": text,
        "char_count": len(text),
        "image_count": count,
        "manifest_items": items,
    }
    if action_type != "post":
        payload["post_url"] = post_url
        payload["target_post_id"] = "123"
    intent = WriteIntent(
        action_type=action_type,
        target_type=target_type,
        target_id=target_id,
        risk_meta=risk,
        compensation=compensation,
        semantic_variant=text_hash(text) + ":media",
        payload=payload,
        actor_identity="@actor",
    )
    return intent, paths


def _executor(
    tmp_path: Path,
    *,
    broker: _MediaBroker,
    content: _ContentEvidence,
    media: _MediaEvidence,
) -> tuple[M5MediaExecutor, EffectLedger]:
    cfg = WebWireConfig(state_dir=tmp_path / "state", kill_env_var=None)
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
    return (
        M5MediaExecutor(
            runtime=runtime,
            content_evidence=content,
            media_evidence=media,
        ),
        ledger,
    )


async def test_post_media_success_records_exact_evidence(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=2)
    broker = _MediaBroker()
    content = _ContentEvidence()
    media = _MediaEvidence(count=2)
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        content=content,
        media=media,
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert media.urls == ["https://x.com/actor/status/999"]
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]
    terminal = ledger.read_records()[-1]
    assert terminal.details["media_count_expected"] == 2
    assert terminal.details["media_count_observed"] == 2
    assert terminal.details["media_count_verified"] is True
    assert terminal.details["source_byte_equivalence_verified"] is False


async def test_reply_media_opens_target_before_any_attachment(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="reply", count=1, post_url="")
    broker = _MediaBroker()
    content = _ContentEvidence()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        content=content,
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert broker.events.index("open_reply:123:https://x.com/i/status/123") < broker.events.index(
        "attach:image-0.jpg"
    )
    assert content.capture_exclude == {"123"}
    assert ledger.read_records()[-1].state is EffectState.EFFECT_CONFIRMED


async def test_quote_media_requires_explicit_quote_attachment_evidence(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="quote", count=1)
    broker = _MediaBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        content=_ContentEvidence(strong=False),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["reconciliation_required"] is True
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_post_submit_media_count_mismatch_records_unknown(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=2)
    executor, ledger = _executor(
        tmp_path,
        broker=_MediaBroker(),
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    terminal = ledger.read_records()[-1]
    assert terminal.state is EffectState.EFFECT_UNKNOWN
    assert terminal.details["media_count_expected"] == 2
    assert terminal.details["media_count_observed"] == 1
    assert terminal.details["media_count_verified"] is False


async def test_uncertain_submit_can_confirm_from_strong_media_evidence(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=1)
    executor, ledger = _executor(
        tmp_path,
        broker=_MediaBroker(mode="uncertain_after_gate"),
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].details["submit_result_ok"] is False


async def test_media_changed_after_approval_is_clean_precommit(tmp_path: Path) -> None:
    intent, paths = _intent(tmp_path, action_type="post", count=1)
    paths[0].write_bytes(b"changed-after-approval")
    broker = _MediaBroker()
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_precommit_attachment_count_mismatch_never_scopes_effect(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=1)
    broker = _MediaBroker(mode="precommit_count_mismatch")
    executor, ledger = _executor(
        tmp_path,
        broker=broker,
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.submit_calls == 0
    assert broker.cleanup_calls == 1
    assert ledger.read_records() == []


async def test_submit_interruption_after_gate_records_unknown(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=1)
    executor, ledger = _executor(
        tmp_path,
        broker=_MediaBroker(mode="raise_after_gate"),
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert execution.result.data["reconciliation_required"] is True
    assert ledger.read_records()[-1].state is EffectState.EFFECT_UNKNOWN


async def test_empty_text_media_post_can_confirm(tmp_path: Path) -> None:
    intent, _ = _intent(tmp_path, action_type="post", count=1, text="")
    executor, ledger = _executor(
        tmp_path,
        broker=_MediaBroker(),
        content=_ContentEvidence(),
        media=_MediaEvidence(count=1),
    )

    execution = await executor.execute(intent)

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert ledger.read_records()[-1].state is EffectState.EFFECT_CONFIRMED
