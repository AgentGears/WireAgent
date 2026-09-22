"""reply_multi_image capability — reply to a post with text + multiple
images (v0.2 M4b).

Thin composition over the shared media-compose harness
(safety/media_compose.run_media_compose) with the REPLY target-context hook:
the reply context opens on the target BEFORE any media attaches (M3a lesson,
encoded in the harness hook contract). The ordered media-manifest transaction
and its five gates are the harness's — one implementation shared with M4a's
post_multi_image, exactly what the B-with-gate decision required.

Result codes:
- reply_multi_image_posted_and_verified (text + media count + thread target)
- reply_multi_image_posted_text_verified_media_count_mismatch
- degraded: submit_clicked_verification_pending / posted_url_captured_...
- target_not_found_before_reply, attachment_* / pre_submit_mismatch /
  killed_before_submit (harness gates)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.media_compose import MediaComposeSpec, PostSubmitHooks, run_media_compose
from webwire.safety.media_manifest import preflight_manifest
from webwire.safety.media_verify import (
    count_post_media as _count_post_media,
)
from webwire.safety.media_verify import (
    verify_post_text as _verify_text,
)
from webwire.safety.media_verify import (
    verify_reply_in_thread as _verify_reply_in_thread,
)
from webwire.safety.post_submit import capture_new_post_id, capture_pre_submit_ids
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["ReplyMultiImageCapability"]


class ReplyMultiImageCapability:
    """Reply to a post with text + multiple images. Ordered manifest
    transaction via the shared harness, target context first."""

    name = "reply_multi_image"

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
        image_paths = input.get("image_paths", [])
        alt_texts = input.get("alt_texts")
        normalized = normalize_text(raw_text)

        # Gate #1 (in the harness too, at execute time): preflight ALL before ANY.
        manifest = preflight_manifest(image_paths, alt_texts=alt_texts)

        meta, comp = DEFAULT_REGISTRY.require("reply")
        # action_type is the BASE action "reply" (P0 rate-limit fix): media
        # replies share the reply budget. Media identity lives in the variant
        # (text hash + manifest hash); the target is in target_id.
        return WriteIntent(
            action_type="reply",
            target_type="post",
            target_id=str(target_post_id),
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized) + ":" + manifest.combined_hash[:16],
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
                "post_url": post_url,
                "target_post_id": str(target_post_id),
                "image_count": manifest.count,
                "manifest_items": [
                    {
                        "index": item.index,
                        "basename": item.attachment.basename,
                        "sha256": item.sha256,
                        "mime": item.attachment.mime,
                        "dimensions": item.attachment.dimensions_str(),
                        "source_path": item.source_path,
                        "alt_text": item.attachment.alt_text,
                        "exif_warnings": item.attachment.exif_warnings,
                    }
                    for item in manifest
                ],
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        count = intent.payload.get("image_count", 0)
        items = intent.payload.get("manifest_items", [])
        warnings = []
        for item in items:
            for w in item.get("exif_warnings", []):
                warnings.append(f"Image {item['index']}: {w}")
            if item.get("exif_has_gps"):
                warnings.append(f"Image {item['index']}: GPS coordinates detected")
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            warnings.append(f"Text exceeds limit ({char_count} > 280)")

        item_summaries = [f"[{it['index']}] {it['basename']} ({it['mime']}, {it['dimensions']})" for it in items]
        return PreviewResult(
            summary=(
                f"REPLY with {count} images to post {target_post_id}: "
                f"'{normalized[:50]}' + {', '.join(item_summaries)}"
            ),
            target_url=intent.payload.get("post_url"),
            current_state=f"replying with {count} images to {target_post_id}",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Reply with media is public; media may be copied before deletion."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Delegate to the shared harness with the reply hook, then verify
        the reply's target relationship in the thread (honest: thread read-back)
        and shape this capability's reporting vocabulary."""
        items_data = intent.payload.get("manifest_items", [])
        expected_count = intent.payload.get("image_count", 0)
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        post_url = intent.payload.get("post_url", "")

        spec = MediaComposeSpec(
            normalized_text=normalized,
            items=items_data,
            expected_count=expected_count,
            target_post_url=post_url,
            target_post_id=target_post_id,
            exclude_ids=frozenset({target_post_id} if target_post_id else ()),
        )
        hooks = PostSubmitHooks(
            capture_pre_submit_ids=capture_pre_submit_ids,
            capture_new_post_id=capture_new_post_id,
            verify_text=_verify_text,
            count_media=_count_post_media,
        )
        r = await run_media_compose(broker, spec, hooks)
        if not r.ok:
            return r  # failure / degraded — harness already shaped it

        outcome = (r.data or {}).get("outcome", {})
        posted_url = outcome.get("posted_url")
        posted_post_id = outcome.get("posted_post_id")
        text_ok = outcome.get("text_verified", False)
        media_count = outcome.get("media_count", 0)

        # Target-relationship verification: the reply must appear in the
        # target's thread (thread read-back; honest about what DOM proves).
        thread = await _verify_reply_in_thread(
            broker, post_url, target_post_id, posted_post_id, normalized,
        )

        media_items_verified = []
        for item in items_data:
            media_items_verified.append({
                "index": item["index"],
                "source_digest": item["sha256"],
                "attachment_ready_verified": True,
                "resulting_attachment_dom_verified": media_count >= expected_count,
                "source_byte_equivalence_verified": False,
            })

        all_media = media_count >= expected_count
        if text_ok and all_media and thread["found"] and thread["text_matches"]:
            result_code = "reply_multi_image_posted_and_verified"
        elif text_ok and all_media and not (thread["found"] and thread["text_matches"]):
            result_code = "reply_multi_image_posted_media_verified_target_unverified"
        else:
            result_code = "reply_multi_image_posted_text_verified_media_count_mismatch"

        return ok_result(data={
            "result": result_code,
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "target_post_id": target_post_id,
            "submitted_text": normalized,
            "media_count_expected": expected_count,
            "media_count_result": media_count,
            "media_count_verified": media_count == expected_count,
            "media_order_verified": False,  # honest — X DOM doesn't expose per-image identity
            "all_media_attachments_verified": all_media,
            "media_items": media_items_verified,
            "target_verified_in_thread": thread["found"] and thread["text_matches"],
            "target_verified_by": "thread_readback" if thread["found"] else "execution_path",
            "source_byte_equivalence_verified": False,
            "verified_by": "identity_aware_post_submit",
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
