"""Transitional WriteKernel adapter for all M5-migrated media capabilities.

The legacy WriteKernel continues to own compose/preview/confirmation during
Layer-5 migration. This adapter canonicalizes the three historical single-photo
payloads into the same ordered ``manifest_items`` schema used by multi-image
capabilities *before* preview and confirmation. After confirmation, the original
capability never receives the legacy mutation broker; execution is delegated to
``M5MediaExecutor``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_media_executor import M5MediaExecution, M5MediaExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5MediaCapabilityAdapter", "M5_MEDIA_CAPABILITIES"]

M5_MEDIA_CAPABILITIES = frozenset(
    {
        "post_photo",
        "reply_photo",
        "quote_photo",
        "post_multi_image",
        "reply_multi_image",
        "quote_multi_image",
    }
)
_SINGLE_PHOTO_CAPABILITIES = frozenset(
    {"post_photo", "reply_photo", "quote_photo"}
)


class M5MediaCapabilityAdapter:
    """Hide legacy mutation authority and canonicalize approved media manifests."""

    def __init__(self, capability: Any, executor: M5MediaExecutor) -> None:
        name = getattr(capability, "name", "")
        if name not in M5_MEDIA_CAPABILITIES:
            raise ValueError(f"unsupported M5 media capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5MediaExecution]] = ContextVar(
            f"m5_media_execution_{name}_{id(self)}",
            default=None,
        )
        self.name = name

    @property
    def tier(self) -> Any:
        return self._capability.tier

    def compose(
        self,
        input: dict[str, Any],
        actor_identity: Optional[str],
    ) -> WriteIntent:
        intent = self._capability.compose(input, actor_identity)
        if self.name in _SINGLE_PHOTO_CAPABILITIES:
            self._canonicalize_single_photo(intent)
        return intent

    async def preview(self, intent: WriteIntent, broker: Any) -> Any:
        return await self._capability.preview(intent, broker)

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Execute through M5; the legacy kernel broker is intentionally ignored."""
        del broker
        execution = await self._executor.execute(intent)
        if not execution.result.ok:
            self._execution.set(None)
            return execution.result
        self._execution.set(execution)
        return execution.result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Expose the exact M5 content + media evidence already terminalized."""
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 media verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is not AttemptState.EFFECT_CONFIRMED:
            return soft_failure(
                "M5 media execution did not reach EFFECT_CONFIRMED",
                failure_category=FailureCategory.UNKNOWN,
            )
        if (
            execution.verification is None
            or not execution.verification.ok
            or execution.media_verification is None
            or not execution.media_verification.ok
        ):
            return soft_failure(
                "M5 media execution lacks complete terminal evidence",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(
            data={
                "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                "content_evidence": execution.verification.data,
                "media_evidence": execution.media_verification.data,
            }
        )

    @staticmethod
    def _canonicalize_single_photo(intent: WriteIntent) -> None:
        payload = intent.payload
        if not isinstance(payload, dict):
            return
        if "manifest_items" in payload:
            return
        source_path = payload.get("image_path")
        digest = payload.get("image_sha256")
        if not isinstance(source_path, str) or not isinstance(digest, str):
            return
        payload["image_count"] = 1
        payload["manifest_items"] = [
            {
                "index": 0,
                "source_path": source_path,
                "sha256": digest,
                "basename": payload.get("image_basename", ""),
                "mime": payload.get("image_mime", ""),
                "dimensions": payload.get("image_dimensions", ""),
                "alt_text": payload.get("image_alt_text", ""),
                "exif_warnings": list(payload.get("image_exif_warnings", []) or []),
            }
        ]
