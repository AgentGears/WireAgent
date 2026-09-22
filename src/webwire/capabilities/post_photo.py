"""post_photo capability — post text + an image to X (v0.2 M1).

Extends the proven post_text pipeline with media. ChatGPT's 8 media-specific
safety concerns are enforced:

1. File bytes binding: SHA-256 in the intent hash + recomputed before upload.
2. Filesystem restriction: validate_media_file with upload roots.
3. Content validation: MIME from magic bytes, size/dimension limits.
4. EXIF metadata: detected, warned in preview.
5. Preview shows image details (basename, dimensions, MIME, digest, EXIF warnings).
6. Upload as state machine: attach_media waits for preview, verify_attachment_ready waits for processing.
7. Composition atomicity: if text or media fails, abort before submit.
8. Verification honest about transcoding: attachment presence verified, not byte equivalence.

Pipeline: compose(validate media) → preview → policy → confirm → attach_media
→ fill text → verify_attachment_ready → read-back text → kill check → submit
→ capture URL → verify text + attachment.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.attachment import (
    file_sha256,
    validate_media_file,
)
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["PostPhotoCapability"]


class PostPhotoCapability:
    """Post text + an image. PUBLIC_CONTENT_IRREVERSIBLE tier with media."""

    name = "post_photo"

    @property
    def tier(self):
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
        # Dedupe key includes media hash (ChatGPT: same caption + different images ≠ duplicate).
        # action_type is the BASE action "post" (P0 rate-limit fix, 2026-09-22):
        # media posts draw from the same per-action budget as text posts and
        # count against the same "3 posts/hour" cap. Media identity lives in
        # semantic_variant; the journal's `capability` field keeps the name.
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
        """Full media post execution."""
        normalized = intent.payload.get("normalized_text", "")
        image_path = intent.payload.get("image_path", "")
        expected_sha256 = intent.payload.get("image_sha256", "")

        # ChatGPT concern #1: recompute digest before upload — fail if changed.
        try:
            current_sha256 = file_sha256(image_path)
        except OSError as exc:
            return _failure("media_changed_after_confirmation",
                            f"Cannot read file {image_path}: {exc!r}")
        if current_sha256 != expected_sha256:
            return _failure("media_changed_after_confirmation",
                            f"File changed since confirmation. "
                            f"Expected {expected_sha256[:12]}, got {current_sha256[:12]}.")

        # Open compose page.
        if not hasattr(broker, "fill_composer"):
            return _failure("pre_submit_mismatch", "Broker missing fill_composer.")
        fill_r = await broker.fill_composer(normalized)
        if not fill_r.ok:
            return _failure("pre_submit_mismatch", f"Could not fill composer: {fill_r.error}")

        # ChatGPT concern #6: attach media + state machine.
        attach_r = await broker.attach_media(image_path)
        if not attach_r.ok:
            return _failure("attachment_upload_failed",
                            f"Could not attach media: {attach_r.error.message if attach_r.error else attach_r}")

        # ChatGPT concern #6: verify attachment ready (no processing spinner).
        ready_r = await broker.verify_attachment_ready()
        if not ready_r.ok:
            return _failure("attachment_not_ready",
                            f"Attachment not ready for submit: {ready_r.error}")

        # ChatGPT concern #7: composition atomicity — read back text + verify.
        read_r = await broker.read_composer_text()
        if not read_r.ok:
            return _failure("pre_submit_mismatch", "Could not read composer text.")
        composer_text = (read_r.data or {}).get("composer_text", "")
        if _normalize_for_compare(composer_text) != _normalize_for_compare(normalized):
            return _failure("pre_submit_mismatch",
                            "Composer text mismatch. NO submit clicked.")

        # ChatGPT concern: final kill-switch check.
        if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
            return _failure("killed_before_submit",
                            "Kill switch tripped. NO submit clicked.")

        # Submit.
        submit_r = await broker.click_submit()
        if not submit_r.ok:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but result uncertain.",
                             normalized)

        # Capture posted URL.
        await asyncio.sleep(5)
        capture_r = await broker.capture_posted_url()
        capture_data = capture_r.data or {} if capture_r.ok else {}
        posted_url = capture_data.get("posted_url")
        posted_post_id = capture_data.get("posted_post_id")

        if not posted_url:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but posted URL not captured.",
                             normalized)

        # Verification: read back + check attachment presence.
        read_back_text = await _read_back_post_text(broker, posted_url)
        if read_back_text is None:
            return _degraded("posted_url_captured_verification_failed",
                             f"Posted URL captured ({posted_url}) but read-back failed.",
                             normalized, posted_url, posted_post_id)
        if _normalize_for_compare(read_back_text) != _normalize_for_compare(normalized):
            return _degraded("posted_url_captured_verification_failed",
                             "Text mismatch on read-back.",
                             normalized, posted_url, posted_post_id)

        # ChatGPT concern #8: verify attachment presence (not byte equivalence).
        attachment_verified = await _verify_attachment_presence(broker, posted_url)

        return ok_result(data={
            "result": "posted_and_verified" if attachment_verified else "posted_text_verified_media_unverified",
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "submitted_text": normalized,
            "image_basename": intent.payload.get("image_basename"),
            "image_sha256": expected_sha256,
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_for_compare(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", text.strip())


def _failure(code: str, message: str) -> ActionResult:
    from super_browser.results import ActionError, ErrorCategory, action_result
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.SECURITY, message, recoverable=False,
    ))
    r.data = {"result": code, "message": message, "public_side_effect": False}
    return r


def _degraded(code: str, message: str, normalized: str,
              posted_url: Optional[str] = None,
              posted_post_id: Optional[str] = None) -> ActionResult:
    from super_browser.results import ActionError, ErrorCategory, action_result
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message,
        "public_side_effect": True,
        "submitted_text": normalized,
        "posted_url": posted_url, "posted_post_id": posted_post_id,
        "supports_compensation": False,
    }
    return r


async def _read_back_post_text(broker: Any, posted_url: str) -> Optional[str]:
    try:
        import asyncio
        if hasattr(broker, "_sb"):
            nav = await broker._sb.navigate(posted_url, wait_until="domcontentloaded")
            if not nav.ok:
                return None
            await asyncio.sleep(4)
            cdp = broker._sb._controller._cdp
            expr = (
                '(function(){var t=document.querySelector("[data-testid=\'tweetText\']");'
                'return t?t.innerText:null;})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                return result.data.get("result", {}).get("value")
        return None
    except Exception:  # noqa: BLE001
        return None


async def _verify_attachment_presence(broker: Any, posted_url: str) -> bool:
    """Verify the posted photo has an image attachment (ChatGPT concern #8).

    Checks for [data-testid='tweetPhoto'] on the posted page. X transcodes
    images, so this verifies PRESENCE not byte equivalence.
    """
    try:
        if hasattr(broker, "_sb"):
            cdp = broker._sb._controller._cdp
            expr = (
                '(function(){'
                'var photo=document.querySelector("[data-testid=\'tweetPhoto\']");'
                'return photo?"present":"absent";'
                '})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                return result.data.get("result", {}).get("value") == "present"
        return False
    except Exception:  # noqa: BLE001
        return False
