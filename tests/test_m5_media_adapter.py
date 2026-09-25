"""WriteKernel integration tests for the Layer-5 media adapter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from super_browser.results.types import FailureCategory

from webwire.capabilities.base import CapabilityTier
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.execution_models import AttemptState
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_media_adapter import M5_MEDIA_CAPABILITIES, M5MediaCapabilityAdapter
from webwire.safety.m5_media_executor import M5MediaExecution
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.text_normalize import text_hash
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.write_kernel import PreviewResult, WriteKernel


class _OriginalMediaCapability:
    def __init__(self, name: str) -> None:
        self.name = name
        self.execute_calls = 0
        self.verify_calls = 0

    @property
    def tier(self) -> CapabilityTier:
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        if self.name.startswith("reply"):
            action_type = "reply"
        elif self.name.startswith("quote"):
            action_type = "quote"
        else:
            action_type = "post"
        risk, compensation = DEFAULT_REGISTRY.require(action_type)
        target_type = "none" if action_type == "post" else "post"
        target_id = "none" if action_type == "post" else "123"
        payload: dict[str, Any] = {
            "normalized_text": input.get("text", "approved media"),
            "char_count": len(input.get("text", "approved media")),
        }
        if action_type != "post":
            payload["post_url"] = "https://x.com/target/status/123"
            payload["target_post_id"] = "123"
        if self.name.endswith("multi_image"):
            payload["image_count"] = 2
            payload["manifest_items"] = [
                {
                    "index": 0,
                    "source_path": "/tmp/a.jpg",
                    "sha256": "a" * 64,
                },
                {
                    "index": 1,
                    "source_path": "/tmp/b.jpg",
                    "sha256": "b" * 64,
                },
            ]
        else:
            payload.update(
                {
                    "image_path": "/tmp/a.jpg",
                    "image_basename": "a.jpg",
                    "image_sha256": "a" * 64,
                    "image_mime": "image/jpeg",
                    "image_dimensions": "100x100",
                    "image_exif_warnings": [],
                }
            )
        return WriteIntent(
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            risk_meta=risk,
            compensation=compensation,
            semantic_variant=text_hash(payload["normalized_text"]) + ":media",
            payload=payload,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        return PreviewResult(summary=f"preview {self.name}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.execute_calls += 1
        raise AssertionError("original media execute must not receive legacy broker")

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        self.verify_calls += 1
        raise AssertionError("original media verify must not receive legacy broker")


class _FakeMediaExecutor:
    def __init__(self, *, unknown: bool = False) -> None:
        self.unknown = unknown
        self.calls: list[WriteIntent] = []

    async def execute(self, intent: WriteIntent) -> M5MediaExecution:
        self.calls.append(intent)
        if self.unknown:
            result = hard_failure(
                "media outcome unknown",
                failure_category=FailureCategory.UNKNOWN,
            )
            result.data = {
                "public_side_effect": True,
                "reconciliation_required": True,
                "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            }
            return M5MediaExecution(
                result=result,
                verification=None,
                media_verification=None,
                attempt_state=AttemptState.EFFECT_UNKNOWN,
                permit_issued=True,
                permit_consumed=True,
            )
        content = ok_result(data={"text_matches": True})
        media = ok_result(data={"media_count": len(intent.payload["manifest_items"])})
        return M5MediaExecution(
            result=ok_result(
                data={
                    "result": "media_content_posted_and_verified",
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            ),
            verification=content,
            media_verification=media,
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
    name: str = "post_photo",
    unknown: bool = False,
) -> tuple[
    WriteKernel,
    M5MediaCapabilityAdapter,
    _OriginalMediaCapability,
    _FakeMediaExecutor,
    DedupeStore,
]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    capability = _OriginalMediaCapability(name)
    executor = _FakeMediaExecutor(unknown=unknown)
    adapter = M5MediaCapabilityAdapter(capability, executor)  # type: ignore[arg-type]
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
    adapter: M5MediaCapabilityAdapter,
) -> ActionResult:
    first = await kernel.execute(
        adapter,
        _ReadBroker(),  # type: ignore[arg-type]
        {"text": "approved media"},
        actor_identity="@actor",
    )
    assert first.data["policy"]["verdict"] == "confirmation_required"
    token = first.data["data"]["confirmation_token"]
    return await kernel.execute(
        adapter,
        _ReadBroker(),  # type: ignore[arg-type]
        {"text": "approved media", "confirmation_token": token},
        actor_identity="@actor",
    )


@pytest.mark.parametrize("name", sorted(M5_MEDIA_CAPABILITIES))
def test_adapter_accepts_all_six_media_capabilities(name: str) -> None:
    capability = _OriginalMediaCapability(name)
    adapter = M5MediaCapabilityAdapter(capability, _FakeMediaExecutor())  # type: ignore[arg-type]
    assert adapter.name == name


def test_single_photo_manifest_is_canonicalized_before_confirmation() -> None:
    capability = _OriginalMediaCapability("reply_photo")
    adapter = M5MediaCapabilityAdapter(capability, _FakeMediaExecutor())  # type: ignore[arg-type]

    intent = adapter.compose({"text": "hello"}, "@actor")

    assert intent.payload["image_count"] == 1
    assert intent.payload["manifest_items"] == [
        {
            "index": 0,
            "source_path": "/tmp/a.jpg",
            "sha256": "a" * 64,
            "basename": "a.jpg",
            "mime": "image/jpeg",
            "dimensions": "100x100",
            "alt_text": "",
            "exif_warnings": [],
        }
    ]


def test_multi_image_manifest_is_not_rewritten() -> None:
    capability = _OriginalMediaCapability("post_multi_image")
    adapter = M5MediaCapabilityAdapter(capability, _FakeMediaExecutor())  # type: ignore[arg-type]

    intent = adapter.compose({"text": "hello"}, "@actor")
    manifest = intent.payload["manifest_items"]

    assert intent.payload["image_count"] == 2
    assert manifest[0]["sha256"] == "a" * 64
    assert manifest[1]["sha256"] == "b" * 64
    assert "basename" not in manifest[0]


async def test_media_adapter_bypasses_original_legacy_methods(tmp_path: Path) -> None:
    kernel, adapter, capability, executor, _ = _kernel(tmp_path)

    result = await _confirm_and_execute(kernel, adapter)

    assert result.data["policy"]["verdict"] == "allow"
    assert result.data["trace"]["execute_ok"] is True
    assert result.data["trace"]["verify_ok"] is True
    assert capability.execute_calls == 0
    assert capability.verify_calls == 0
    assert len(executor.calls) == 1
    assert executor.calls[0].payload["manifest_items"][0]["sha256"] == "a" * 64


async def test_media_unknown_marks_dedupe_and_never_calls_legacy_methods(
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
    assert len(executor.calls) == 1
    intent = adapter.compose({"text": "approved media"}, "@actor")
    assert dedupe.check(intent.dedupe_key()) is False
