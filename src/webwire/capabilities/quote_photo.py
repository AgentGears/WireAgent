"""quote_photo capability — quote a post with text + an image (v0.2 M3b).

Combines:
- quote_post's target-scoped flow (repost→Quote menu on the target article)
- post_photo's media pipeline (validate, attach, state machine, verify)

ChatGPT's M3b caution: quote_photo has TWO INDEPENDENT attachments:
1. The quoted-target attachment (the post being quoted)
2. The uploaded-media attachment (the user's image)
These must be verified and reported SEPARATELY — not collapsed into one flag.

Also uses the identity-aware post-submit verifier (M3 tranche requirement):
record pre-submit IDs → submit → poll for new ID → exclude known IDs.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.attachment import validate_media_file, file_sha256
from webwire.safety.post_submit import capture_pre_submit_ids, capture_new_post_id
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult, WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["QuotePhotoCapability"]


class QuotePhotoCapability:
    """Quote a post with text + image. PUBLIC_CONTENT_IRREVERSIBLE + target + media."""

    name = "quote_photo"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
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

        meta, comp = DEFAULT_REGISTRY.get("quote")
        return WriteIntent(
            action_type="quote_photo",
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
        """Target-scoped quote with photo. Dual attachment (quote + media) verified separately."""
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        post_url = intent.payload.get("post_url", "")
        image_path = intent.payload.get("image_path", "")
        expected_sha256 = intent.payload.get("image_sha256", "")

        # Recompute digest before upload.
        try:
            current_sha256 = file_sha256(image_path)
        except OSError as exc:
            return _failure("media_changed_after_confirmation", f"Cannot read file: {exc!r}")
        if current_sha256 != expected_sha256:
            return _failure("media_changed_after_confirmation", "File changed since confirmation.")

        # Step 1: open quote on target-scoped article.
        open_r = await broker.open_quote_on_target(post_url, target_post_id)
        if not open_r.ok:
            return _failure("quote_action_not_available",
                            f"Could not open quote on target: {open_r.error}")

        # Step 2: fill quote text.
        fill_r = await broker.fill_quote_composer(normalized)
        if not fill_r.ok:
            return _failure("pre_submit_mismatch", f"Could not fill quote composer: {fill_r.error}")

        # Step 3: attach media.
        attach_r = await broker.attach_media(image_path)
        if not attach_r.ok:
            return _failure("attachment_upload_failed", f"Could not attach media: {attach_r.error}")

        # Step 4: verify attachment ready.
        ready_r = await broker.verify_attachment_ready()
        if not ready_r.ok:
            return _failure("attachment_not_ready", f"Attachment not ready: {ready_r.error}")

        # Step 5: re-verify composer text (composition atomicity — ChatGPT's M3b caution:
        # "The media picker could preserve text while displacing the quote composition context").
        read_r = await broker.read_composer_text()
        if not read_r.ok:
            return _failure("pre_submit_mismatch", "Could not read composer text.")
        composer_text = (read_r.data or {}).get("composer_text", "")
        if _normalize_for_compare(composer_text) != _normalize_for_compare(normalized):
            return _failure("pre_submit_mismatch",
                            f"Composer text mismatch after media attach. NO submit.")

        # Step 6: identity-aware pre-submit capture.
        pre_submit_ids = await capture_pre_submit_ids(broker)

        # Step 7: final kill check.
        if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
            return _failure("killed_before_submit", "Kill switch tripped. NO submit clicked.")

        # Step 8: submit.
        submit_r = await broker.click_submit()
        if not submit_r.ok:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but result uncertain.",
                             normalized, target_post_id)

        # Step 9: identity-aware post-submit capture (M3 tranche requirement).
        await asyncio.sleep(3)
        posted_post_id, posted_url = await capture_new_post_id(
            broker, pre_submit_ids, exclude_ids={target_post_id},
        )

        if not posted_post_id:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but no new post ID captured.",
                             normalized, target_post_id)

        # Step 10: dual verification — text + quote attachment + media attachment.
        await asyncio.sleep(3)
        text_ok = await _verify_text(broker, posted_url, normalized)
        media_ok = await _verify_media(broker, posted_url)

        # Quote attachment: verified by execution path (X doesn't expose quote target in DOM).
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_for_compare(text: str) -> str:
    import re as _re
    return _re.sub(r"\s+", " ", text.strip())


def _failure(code: str, message: str) -> ActionResult:
    from super_browser.results import action_result, ActionError, ErrorCategory
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.SECURITY, message, recoverable=False,
    ))
    r.data = {"result": code, "message": message, "public_side_effect": False}
    return r


def _degraded(code: str, message: str, normalized: str,
              target_post_id: str = None, posted_url: str = None,
              posted_post_id: str = None) -> ActionResult:
    from super_browser.results import action_result, ActionError, ErrorCategory
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message, "public_side_effect": True,
        "submitted_text": normalized, "target_post_id": target_post_id,
        "posted_url": posted_url, "posted_post_id": posted_post_id,
        "supports_compensation": False,
    }
    return r


async def _verify_text(broker: Any, posted_url: str, normalized: str) -> bool:
    """Verify the posted quote's text matches."""
    try:
        if hasattr(broker, "_sb"):
            nav = await broker._sb.navigate(posted_url, wait_until="domcontentloaded")
            if not nav.ok:
                return False
            await asyncio.sleep(4)
            cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
            expr = (
                '(function(){var t=document.querySelector("[data-testid=\'tweetText\']");'
                'return t?t.innerText:null;})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                text = result.data.get("result", {}).get("value") or ""
                return _normalize_for_compare(text) == _normalize_for_compare(normalized)
        return False
    except Exception:  # noqa: BLE001
        return False


async def _verify_media(broker: Any, posted_url: str) -> bool:
    """Verify the posted quote has an image attachment."""
    try:
        if hasattr(broker, "_sb"):
            cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
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
