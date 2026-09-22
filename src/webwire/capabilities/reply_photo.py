"""reply_photo capability — reply to a post with text + an image (v0.2 M3a).

M4b-extraction follow-up (2026-09-23, C4): execute() DELEGATES to the shared
harness `safety.media_compose.run_media_compose` with the REPLY context hook
(target opens BEFORE media — the M3a lesson is the hook contract) and a
single-item manifest, exactly like reply_multi_image. The single-item path
GAINS the exact-count gate it previously lacked (accepted tightening).

Post-harness: thread-aware TARGET verification (the reply must appear in the
target's thread) via the shared verify_reply_in_thread, reported honestly.

Result codes:
- reply_photo_posted_and_target_verified (text + media + thread target)
- reply_photo_posted_text_verified_media_unverified
- degraded submit_clicked_verification_pending / posted_url_captured_...
- target_not_found_before_reply, attachment_* / pre_submit_mismatch /
  killed_before_submit (harness gates)
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.attachment import validate_media_file
from webwire.safety.media_compose import MediaComposeSpec, PostSubmitHooks, run_media_compose
from webwire.safety.media_verify import (
    count_post_media as _count_post_media,
)
from webwire.safety.media_verify import (
    verify_post_text as _verify_text,
)
from webwire.safety.media_verify import (
    verify_reply_in_thread as _verify_reply_in_thread,
)
from webwire.safety.post_submit import capture_pre_submit_ids
from webwire.safety.text_normalize import normalize_text, text_hash
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["ReplyPhotoCapability"]


async def _capture_reply_url(broker: Any, target_post_id: str):
    """Find the first non-target status href on the page (the reply).
    Historical capture method, preserved verbatim as the harness adapter's
    inner step."""
    try:
        if hasattr(broker, "_sb"):
            cdp = broker._sb._controller._cdp
            expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                'var seen={};'
                'for(var i=0;i<links.length;i++){'
                'var href=links[i].getAttribute("href");'
                'if(href&&href.indexOf("/status/")>=0&&!seen[href]){'
                'seen[href]=1;var m=href.match(/\\/status\\/(\\d+)/);'
                f'if(m&&m[1]!=="{target_post_id}"){{'
                'return JSON.stringify({{href:href,id:m[1]}});}}}}}}'
                'return null;})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                raw = result.data.get("result", {}).get("value")
                if raw:
                    data = json.loads(raw)
                    href = data.get("href", "")
                    pid = data.get("id")
                    full_url = f"https://x.com{href}" if href.startswith("/") else href
                    return full_url, pid
        return None, None
    except Exception:  # noqa: BLE001
        return None, None


async def _capture_adapter(broker: Any, pre_ids: set, exclude_ids=None):
    """Harness-shaped capture adapter: historical reply-URL capture."""
    target = next(iter(exclude_ids or {""}))
    await asyncio.sleep(5)
    url, pid = await _capture_reply_url(broker, target)
    return pid, url


class ReplyPhotoCapability:
    """Reply to a post with text + image. PUBLIC_CONTENT_IRREVERSIBLE +
    target binding + media."""

    name = "reply_photo"

    @property
    def tier(self):
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        post_url = input.get("post_url") or ""
        target_post_id = input.get("target_post_id") or ""
        if not target_post_id and post_url:
            m = re.search(r"/status/(\d+)", post_url)
            target_post_id = m.group(1) if m else post_url
        raw_text = input.get("text", "")
        image_path = input.get("image_path", "")
        normalized = normalize_text(raw_text)

        attachment = validate_media_file(image_path)

        meta, comp = DEFAULT_REGISTRY.require("reply")
        # action_type is the BASE action "reply" (P0 rate-limit fix): media
        # replies share the reply budget. Media identity lives in the variant.
        return WriteIntent(
            action_type="reply",
            target_type="post",
            target_id=str(target_post_id),
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized) + ":" + attachment.sha256[:16],
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
                "post_url": post_url,
                "target_post_id": str(target_post_id),
                "image_path": attachment.path,
                "image_basename": attachment.basename,
                "image_sha256": attachment.sha256,
                "image_mime": attachment.mime,
                "image_dimensions": attachment.dimensions_str(),
                "image_exif_warnings": attachment.exif_warnings,
                "image_exif_has_gps": attachment.exif_has_gps,
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        warnings = list(intent.payload.get("image_exif_warnings", []))
        if intent.payload.get("image_exif_has_gps"):
            warnings.append("GPS coordinates detected in image EXIF")
        return PreviewResult(
            summary=(
                f"REPLY PHOTO to {target_post_id}: '{normalized[:60]}' "
                f"+ image {intent.payload.get('image_basename')} "
                f"({intent.payload.get('image_mime')}, {intent.payload.get('image_dimensions')})"
            ),
            target_url=intent.payload.get("post_url"),
            current_state=f"replying with photo to {target_post_id}",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Reply with photo is public; media may be copied before deletion."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Delegate to the shared harness (reply hook, single-item manifest),
        then verify the thread-target relationship and shape the result."""
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        post_url = intent.payload.get("post_url", "")

        spec = MediaComposeSpec(
            normalized_text=normalized,
            items=[{
                "index": 0,
                "source_path": intent.payload.get("image_path", ""),
                "sha256": intent.payload.get("image_sha256", ""),
            }],
            expected_count=1,
            target_post_url=post_url,
            target_post_id=target_post_id,
            context="reply",
            exclude_ids=frozenset({target_post_id} if target_post_id else ()),
        )
        hooks = PostSubmitHooks(
            capture_pre_submit_ids=capture_pre_submit_ids,
            capture_new_post_id=_capture_adapter,
            verify_text=_verify_text,
            count_media=_count_post_media,
        )
        r = await run_media_compose(broker, spec, hooks)
        if not r.ok:
            return r

        outcome = (r.data or {}).get("outcome", {})
        posted_url = outcome.get("posted_url")
        posted_post_id = outcome.get("posted_post_id")
        text_ok = outcome.get("text_verified", False)
        media_count = outcome.get("media_count", 0)

        # Thread-aware target verification (Phase 4c-v semantics).
        thread = await _verify_reply_in_thread(
            broker, post_url, target_post_id, posted_post_id, normalized,
        )

        media_verified = media_count >= 1
        if text_ok and media_verified and thread["found"] and thread["text_matches"]:
            result_code = "reply_photo_posted_and_target_verified"
        elif text_ok and media_verified:
            result_code = "reply_photo_posted_text_verified_media_unverified"
        else:
            result_code = "posted_url_captured_verification_failed"

        return ok_result(data={
            "result": result_code,
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "target_post_id": target_post_id,
            "submitted_text": normalized,
            "media_attachment_verified": media_verified,
            "target_verified_in_thread": thread["found"] and thread["text_matches"],
            "target_verified_by": "thread_readback" if thread["found"] else "execution_path",
            "source_byte_equivalence_verified": False,
            "verified_by": "thread_readback" if thread["found"] else "execution_path",
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
            "residual_side_effects": [
                "public_media_may_have_been_observed_or_copied",
                "notifications_may_be_sent",
                "content_may_be_indexed_or_cached",
                "delete_does_not_fully_undo_distribution",
            ],
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        return ok_result(data={"verified": True, "note": "Inline verification in execute."})
