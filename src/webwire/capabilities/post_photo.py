"""post_photo capability — post text + an image to X (v0.2 M1).

M4b-extraction follow-up (2026-09-23, C4): the transaction now lives in the
shared harness `safety.media_compose.run_media_compose` — this capability's
execute() DEMONSTRABLY DELEGATES there with a single-item manifest, exactly
like its multi-image siblings. ChatGPT's 8 media-safety concerns remain
enforced (compose-time validation + the harness gates), and the single-item
path GAINS the exact-count gate it previously lacked (accepted tightening).

Capture adapter: the historical capture_posted_url broker method, wrapped to
the harness's PostSubmitHooks shape. Post-submit verification: the shared
id-scoped verifiers in safety/media_verify.py.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.attachment import validate_media_file
from webwire.safety.media_compose import MediaComposeSpec, PostSubmitHooks, run_media_compose
from webwire.safety.media_verify import (
    count_post_media as _count_post_media,
    verify_post_text as _verify_text,
)
from webwire.safety.post_submit import capture_pre_submit_ids
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult, WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["PostPhotoCapability"]


async def _capture_via_posted_url(broker: Any, pre_ids: set, exclude_ids=None):
    """Harness-shaped capture adapter over the historical broker method."""
    await asyncio.sleep(5)
    r = await broker.capture_posted_url()
    data = r.data or {} if r.ok else {}
    return data.get("posted_post_id"), data.get("posted_url")


class PostPhotoCapability:
    """Post text + an image. PUBLIC_CONTENT_IRREVERSIBLE tier with media."""

    name = "post_photo"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        raw_text = input.get("text", "")
        image_path = input.get("image_path", "")
        normalized = normalize_text(raw_text)

        # Validate the media file NOW (before preview) — ChatGPT concerns #1-5.
        # This is synchronous; it reads the file + computes SHA-256.
        attachment = validate_media_file(image_path)

        meta, comp = DEFAULT_REGISTRY.require("post")
        # action_type is the BASE action "post" (P0 rate-limit fix): media
        # posts draw from the same post budget as text posts. Media identity
        # lives in semantic_variant; dedupe distinguishes same text/different
        # image via the digest.
        return WriteIntent(
            action_type="post",
            target_type="none",
            target_id="none",
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized) + ":" + attachment.sha256[:16],
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
                "image_path": attachment.path,
                "image_basename": attachment.basename,
                "image_sha256": attachment.sha256,
                "image_mime": attachment.mime,
                "image_dimensions": attachment.dimensions_str(),
                "image_alt_text": attachment.alt_text,
                "image_exif_warnings": attachment.exif_warnings,
                "image_exif_has_gps": attachment.exif_has_gps,
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Preview shows text + full image details (ChatGPT concern #5)."""
        normalized = intent.payload.get("normalized_text", "")
        warnings = list(intent.payload.get("image_exif_warnings", []))
        if intent.payload.get("image_exif_has_gps"):
            warnings.append("GPS coordinates detected in image EXIF")
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            warnings.append(f"Text exceeds X's character limit ({char_count} > 280)")

        return PreviewResult(
            summary=(
                f"Will post photo: '{normalized[:60]}' "
                f"+ image {intent.payload.get('image_basename')} "
                f"({intent.payload.get('image_mime')}, {intent.payload.get('image_dimensions')}, "
                f"sha256:{intent.payload.get('image_sha256', '')[:12]})"
            ),
            current_state=f"text ({char_count} chars) + 1 image",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Photo may be seen, copied, or cached before any deletion."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Delegate the transaction to the shared harness (single-item
        manifest), then shape the outcome into this capability's reporting
        vocabulary."""
        normalized = intent.payload.get("normalized_text", "")
        spec = MediaComposeSpec(
            normalized_text=normalized,
            items=[{
                "index": 0,
                "source_path": intent.payload.get("image_path", ""),
                "sha256": intent.payload.get("image_sha256", ""),
            }],
            expected_count=1,
        )
        hooks = PostSubmitHooks(
            capture_pre_submit_ids=capture_pre_submit_ids,
            capture_new_post_id=_capture_via_posted_url,
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

        # ChatGPT concern #8: attachment PRESENCE verified, not byte equivalence.
        attachment_verified = media_count >= 1
        return ok_result(data={
            "result": "posted_and_verified" if attachment_verified
                      else "posted_text_verified_media_unverified",
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "submitted_text": normalized,
            "image_basename": intent.payload.get("image_basename"),
            "image_sha256": intent.payload.get("image_sha256"),
            "media_attachment_verified": attachment_verified,
            "source_byte_equivalence_verified": False,  # X transcodes
            "verified_by": "public_post_attachment_presence",
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
            "residual_side_effects": [
                "public_media_may_have_been_observed_or_copied",
                "content_may_be_indexed_or_cached",
                "delete_does_not_fully_undo_distribution",
            ],
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        return ok_result(data={"verified": True, "note": "Inline verification in execute."})
