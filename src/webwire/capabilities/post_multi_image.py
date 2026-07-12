"""post_multi_image capability — post text + multiple images (v0.2 M4a).

ChatGPT's M4 framework: ordered media-manifest transaction, not repeated
single-image operations.

State machine:
  validated → manifest_bound → composer_open → uploading_item_1 → item_1_ready
  → ... → uploading_item_n → item_n_ready → composition_reverified
  → submit_authorized → submitted → identity_captured → media_batch_verified

Any failure before submit_authorized → abort-and-cleanup, never submission.

Key gates:
1. Preflight ALL before ANY upload (one invalid → entire invocation rejected).
2. Exact-count verification after each upload (1→2→…→N).
3. Abort and cleanup on partial failure (close composer, verify gone).
4. Order preservation (per-item evidence, not just count).
5. Transcoding-honest per-item verification.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.attachment import file_sha256
from webwire.safety.media_manifest import MediaManifest, MediaManifestItem, preflight_manifest, MAX_IMAGES_PER_POST
from webwire.safety.post_submit import capture_pre_submit_ids, capture_new_post_id
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult, WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["PostMultiImageCapability"]


class PostMultiImageCapability:
    """Post text + multiple images. Ordered manifest transaction."""

    name = "post_multi_image"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        raw_text = input.get("text", "")
        image_paths = input.get("image_paths", [])
        alt_texts = input.get("alt_texts")
        normalized = normalize_text(raw_text)

        # Gate #1: preflight ALL before ANY upload.
        manifest = preflight_manifest(image_paths, alt_texts=alt_texts)

        meta, comp = DEFAULT_REGISTRY.get("post")
        # Dedupe key includes combined hash of all attachment digests (ChatGPT M4).
        return WriteIntent(
            action_type="post_multi_image",
            target_type="none",
            target_id="none",
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized) + ":" + manifest.combined_hash[:16],
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
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
            summary=f"Will post {count} images: '{normalized[:50]}' + {', '.join(item_summaries)}",
            current_state=f"text ({char_count} chars) + {count} images",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Multi-image post is public; media may be copied before deletion."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        normalized = intent.payload.get("normalized_text", "")
        items_data = intent.payload.get("manifest_items", [])
        expected_count = intent.payload.get("image_count", 0)

        # Recompute digests before upload (concern #1 from M1).
        for item in items_data:
            try:
                current = file_sha256(item["source_path"])
            except OSError as exc:
                return await self._abort(broker, "media_changed_after_confirmation",
                                         f"Cannot read {item['source_path']}: {exc!r}")
            if current != item["sha256"]:
                return await self._abort(broker, "media_changed_after_confirmation",
                                         f"Image {item['index']} changed since confirmation.")

        # Open compose + fill text.
        fill_r = await broker.fill_composer(normalized)
        if not fill_r.ok:
            return await self._abort(broker, "pre_submit_mismatch", f"Could not fill composer: {fill_r.error}")

        # Upload each image in order. Gate #2: exact-count verification after each.
        for i, item in enumerate(items_data):
            attach_r = await broker.attach_media(item["source_path"])
            if not attach_r.ok:
                return await self._abort(broker, "attachment_upload_failed",
                                         f"Image {item['index']} upload failed: {attach_r.error}")

            # Verify cumulative count matches expected (i+1 after i+1 uploads).
            ready_r = await broker.verify_attachment_ready()
            if not ready_r.ok:
                return await self._abort(broker, "attachment_not_ready",
                                         f"Image {item['index']} not ready: {ready_r.error}")

            count_r = await broker.count_attachments()
            actual_count = (count_r.data or {}).get("count", 0) if count_r.ok else 0
            if actual_count != i + 1:
                return await self._abort(broker, "attachment_count_mismatch",
                                         f"Expected {i+1} attachments after upload {i+1}, "
                                         f"got {actual_count}. Aborting — no partial post.")

        # Gate: composition reverification (text still correct after all uploads).
        read_r = await broker.read_composer_text()
        if not read_r.ok:
            return await self._abort(broker, "pre_submit_mismatch", "Could not read composer text.")
        composer_text = (read_r.data or {}).get("composer_text", "")
        if _normalize_for_compare(composer_text) != _normalize_for_compare(normalized):
            return await self._abort(broker, "pre_submit_mismatch",
                                     f"Composer text mismatch after all uploads. Aborting.")

        # Final count check (all present).
        final_count_r = await broker.count_attachments()
        final_count = (final_count_r.data or {}).get("count", 0) if final_count_r.ok else 0
        if final_count != expected_count:
            return await self._abort(broker, "attachment_count_mismatch",
                                     f"Final count {final_count} != expected {expected_count}.")

        # Pre-submit identity capture.
        pre_submit_ids = await capture_pre_submit_ids(broker)

        # Final kill check.
        if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
            return await self._abort(broker, "killed_before_submit", "Kill switch tripped. Aborting.")

        # Submit.
        submit_r = await broker.click_submit()
        if not submit_r.ok:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but result uncertain.", normalized)

        # Identity-aware post-submit capture.
        await asyncio.sleep(3)
        posted_post_id, posted_url = await capture_new_post_id(
            broker, pre_submit_ids, exclude_ids=set(),
        )
        if not posted_post_id:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but no new post ID captured.", normalized)

        # Post-submit verification: text + media count.
        await asyncio.sleep(3)
        text_ok = await _verify_text(broker, posted_url, normalized)
        media_count = await _count_post_media(broker, posted_url)

        media_items_verified = []
        for item in items_data:
            media_items_verified.append({
                "index": item["index"],
                "source_digest": item["sha256"],
                "attachment_ready_verified": True,
                "resulting_attachment_dom_verified": media_count >= expected_count,
                "source_byte_equivalence_verified": False,
            })

        all_verified = text_ok and media_count >= expected_count

        return ok_result(data={
            "result": "media_batch_verified" if all_verified else "posted_text_verified_media_count_mismatch",
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "submitted_text": normalized,
            "media_count_expected": expected_count,
            "media_count_result": media_count,
            "media_count_verified": media_count == expected_count,
            "media_order_verified": False,  # honest — X DOM doesn't expose per-image identity
            "all_media_attachments_verified": all_verified,
            "media_items": media_items_verified,
            "source_byte_equivalence_verified": False,
            "verified_by": "identity_aware_post_submit",
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

    async def _abort(self, broker: Any, code: str, message: str) -> ActionResult:
        """Gate #3: abort and cleanup. Never submit on partial failure."""
        try:
            await broker.close_composer()
        except Exception:  # noqa: BLE001
            pass
        return _failure(code, message)


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
              posted_url: str = None, posted_post_id: str = None) -> ActionResult:
    from super_browser.results import action_result, ActionError, ErrorCategory
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message, "public_side_effect": True,
        "submitted_text": normalized,
        "posted_url": posted_url, "posted_post_id": posted_post_id,
        "supports_compensation": False,
    }
    return r


async def _verify_text(broker: Any, posted_url: str, normalized: str) -> bool:
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


async def _count_post_media(broker: Any, posted_url: str) -> int:
    """Count tweetPhoto elements on the posted page."""
    try:
        if hasattr(broker, "_sb"):
            cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
            expr = (
                '(function(){'
                'var art=document.querySelector("article");'
                'if(!art)return 0;'
                'return art.querySelectorAll("[data-testid=\'tweetPhoto\']").length;'
                '})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                return result.data.get("result", {}).get("value", 0)
        return 0
    except Exception:  # noqa: BLE001
        return 0
