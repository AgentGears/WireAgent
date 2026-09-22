"""quote_photo capability — quote a post with text + an image (v0.2 M3b).

M4b-extraction follow-up (2026-09-23, C4): execute() DELEGATES to the shared
harness `safety.media_compose.run_media_compose` with the QUOTE context hook
(quote context opens BEFORE media) and a single-item manifest, exactly like
quote_multi_image. The single-item path GAINS the exact-count gate it
previously lacked (accepted tightening).

ChatGPT's M3b caution carried over: TWO INDEPENDENT attachments — the
quoted-target attachment and the uploaded media — verified and reported
SEPARATELY. Quote attachment by execution path (X's DOM does not expose the
quoted target); media by id-scoped DOM count.

Also uses the identity-aware post-submit verifier (M3 tranche requirement).
"""

from __future__ import annotations

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
from webwire.safety.post_submit import capture_new_post_id, capture_pre_submit_ids
from webwire.safety.text_normalize import normalize_text, text_hash
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["QuotePhotoCapability"]


class QuotePhotoCapability:
    """Quote a post with text + image. PUBLIC_CONTENT_IRREVERSIBLE + target + media."""

    name = "quote_photo"

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

        meta, comp = DEFAULT_REGISTRY.require("quote")
        # action_type is the BASE action "quote" (P0 rate-limit fix): media
        # quotes share the quote budget. Media identity lives in the variant.
        return WriteIntent(
            action_type="quote",
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
                f"QUOTE PHOTO of {target_post_id}: QUOTE TEXT '{normalized[:60]}' "
                f"+ image {intent.payload.get('image_basename')} "
                f"({intent.payload.get('image_mime')}, {intent.payload.get('image_dimensions')})"
            ),
            target_url=intent.payload.get("post_url"),
            current_state=f"quoting {target_post_id} with photo",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Quote with photo amplifies the target and creates standalone public content."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Delegate to the shared harness (quote hook, single-item manifest),
        then report the dual attachment honestly."""
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
            context="quote",
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
            return r

        outcome = (r.data or {}).get("outcome", {})
        posted_url = outcome.get("posted_url")
        posted_post_id = outcome.get("posted_post_id")
        text_ok = outcome.get("text_verified", False)
        media_count = outcome.get("media_count", 0)

        media_ok = media_count >= 1
        # Quote attachment: verified by execution path (X doesn't expose the
        # quote target in the DOM) — stated, never overstated.
        quote_attachment_verified = True  # open_quote_on_target succeeded (target-scoped)

        result_code = "quote_photo_posted_and_target_verified"
        if not text_ok:
            result_code = "posted_url_captured_verification_failed"

        return ok_result(data={
            "result": result_code,
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "target_post_id": target_post_id,
            "submitted_text": normalized,
            # Dual attachment — verified SEPARATELY (ChatGPT's M3b caution).
            "quote_target_id": target_post_id,
            "quote_attachment_verified": quote_attachment_verified,
            "quote_attachment_verified_by": "execution_path",
            "quote_attachment_dom_verified": False,
            "media_attachment_verified": media_ok,
            "source_byte_equivalence_verified": False,
            "verified_by": "identity_aware_post_submit" if posted_post_id else "execution_path",
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
            "residual_side_effects": [
                "public_media_may_have_been_observed_or_copied",
                "amplifies_quoted_post",
                "notifications_may_be_sent",
                "content_may_be_indexed_or_cached",
                "delete_does_not_fully_undo_distribution",
            ],
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        return ok_result(data={"verified": True, "note": "Inline verification in execute."})
