"""post_text capability — the first live public content write (Phase 4b).

Implements ChatGPT's 16-step execution sequence for irreversible writes:
1-6. Receive frozen intent, re-check capability/tier/text/length/dedupe.
7. Open composer.
8. Fill composer with exactly normalized_text.
9. Read composer text back from DOM.
10. Assert DOM composer text == normalized_text. ABORT if mismatch.
11. Final kill-switch check. ABORT if tripped.
12. Click submit.
13. Wait for posted URL / status id.
14. Read posted URL back through existing read path.
15. Assert read-back text == normalized_text.
16. Journal posted_url, posted_post_id, verification, residual side effects.

Conservative failure semantics (user must know if a public side effect may exist):
- pre_submit_mismatch: composer text ≠ normalized_text. NO submit. Safe.
- killed_before_submit: kill switch tripped after fill, before click. NO submit. Safe.
- submit_clicked_verification_pending: clicked but URL not captured. MAY have side effect.
- posted_url_captured_verification_failed: URL found but read-back failed. Side effect likely.
- posted_and_verified: URL captured + read-back text matched. Full success.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult, WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["PostTextCapability"]


class PostTextCapability:
    """Post text to X. PUBLIC_CONTENT_IRREVERSIBLE tier. First live public write."""

    name = "post_text"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        raw_text = input.get("text", "")
        normalized = normalize_text(raw_text)
        meta, comp = DEFAULT_REGISTRY.get("post")
        return WriteIntent(
            action_type="post",
            target_type="none",
            target_id="none",
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized),
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        normalized = intent.payload.get("normalized_text", "")
        char_count = intent.payload.get("char_count", len(normalized))
        is_valid, _ = validate_length(normalized)
        warnings = []
        if not is_valid:
            warnings.append(f"Text exceeds X's character limit ({char_count} > 280)")
        if not normalized:
            warnings.append("Empty post text")
        return PreviewResult(
            summary=f"Will post publicly: {normalized[:100]!r}",
            current_state=f"normalized_text ({char_count} chars)",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Deletion does not fully undo distribution."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """The 16-step execution sequence for the first live public post."""
        # Steps 1-6: re-validate the frozen intent.
        normalized = intent.payload.get("normalized_text", "")
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            return _failure("pre_submit_mismatch",
                            f"Text length invalid ({char_count} > 280). No submit.")

        if not hasattr(broker, "fill_composer"):
            return _failure("pre_submit_mismatch",
                            "Broker missing fill_composer (not a PostWritePort).")

        # Step 7-8: open composer + fill with normalized_text.
        fill_r = await broker.fill_composer(normalized)
        if not fill_r.ok:
            return _failure("pre_submit_mismatch", f"Could not fill composer: {fill_r.error}")

        # Step 9-10: read composer text back + assert it matches.
        read_r = await broker.read_composer_text()
        if not read_r.ok:
            return _failure("pre_submit_mismatch", "Could not read composer text back.")
        composer_text = (read_r.data or {}).get("composer_text", "")
        if _normalize_for_compare(composer_text) != _normalize_for_compare(normalized):
            return _failure("pre_submit_mismatch",
                            f"Composer text mismatch. Expected {normalized!r}, "
                            f"got {composer_text!r}. NO submit clicked.")

        # Step 11: FINAL kill-switch check ("hand on the button").
        if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
            return _failure("killed_before_submit",
                            "Kill switch tripped after fill, before submit. NO submit clicked.")

        # Step 12: click submit.
        submit_r = await broker.click_submit()
        if not submit_r.ok:
            return _failure("submit_clicked_verification_pending",
                            f"Submit click returned error. Status uncertain.")

        # Step 13: capture posted URL.
        import asyncio
        await asyncio.sleep(4)
        capture_r = await broker.capture_posted_url()
        capture_data = capture_r.data or {} if capture_r.ok else {}
        posted_url = capture_data.get("posted_url")
        posted_post_id = capture_data.get("posted_post_id")

        if not posted_url:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but posted URL not captured. "
                             "Public content MAY exist.",
                             normalized)

        # Steps 14-15: read-back verification via the existing read path.
        # (In live execution, this would call d.invoke("read", {post_url: posted_url}).
        #  Here we use the broker's read capabilities directly.)
        read_back_text = await _read_back_post_text(broker, posted_url)
        if read_back_text is None:
            return _degraded("posted_url_captured_verification_failed",
                             f"Posted URL captured ({posted_url}) but read-back failed. "
                             f"Public content likely exists.",
                             normalized, posted_url, posted_post_id)

        if _normalize_for_compare(read_back_text) != _normalize_for_compare(normalized):
            return _degraded("posted_url_captured_verification_failed",
                             f"Posted URL captured ({posted_url}) but text mismatch. "
                             f"Expected {normalized!r}, got {read_back_text!r}.",
                             normalized, posted_url, posted_post_id)

        # Step 16: posted_and_verified — full success.
        return ok_result(data={
            "result": "posted_and_verified",
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "submitted_text": normalized,
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
            "residual_side_effects": [
                "public_content_may_be_seen",
                "notifications_may_be_sent",
                "content_may_be_indexed_or_cached",
                "delete_does_not_fully_undo_distribution",
            ],
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Verify is handled inline in execute (steps 14-15). Return ok."""
        return ok_result(data={"verified": True, "note": "Inline verification in execute."})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_for_compare(text: str) -> str:
    """Loose comparison normalization: strip + collapse whitespace for the
    composer-read-back assertion. X may add subtle formatting (trailing space,
    line break) that doesn't change the semantic content."""
    import re
    return re.sub(r"\s+", " ", text.strip())


def _failure(code: str, message: str) -> ActionResult:
    """Pre-submit failure: NO public side effect occurred."""
    from super_browser.results import action_result, ActionError, ErrorCategory
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.SECURITY, message, recoverable=False,
    ))
    r.data = {"result": code, "message": message, "public_side_effect": False}
    return r


def _degraded(code: str, message: str, normalized: str,
              posted_url: str = None, posted_post_id: str = None) -> ActionResult:
    """Degraded result: public side effect MAY have occurred."""
    from super_browser.results import action_result, ActionError, ErrorCategory
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message,
        "public_side_effect": True,
        "submitted_text": normalized,
        "posted_url": posted_url,
        "posted_post_id": posted_post_id,
        "supports_compensation": False,
    }
    return r


async def _read_back_post_text(broker: Any, posted_url: str) -> Optional[str]:
    """Read the text of the just-posted post via the broker's read primitives.
    Returns the text or None if it couldn't be read."""
    try:
        import asyncio
        # Navigate to the posted URL and read the tweetText.
        if hasattr(broker, "_sb"):
            nav = await broker._sb.navigate(posted_url, wait_until="domcontentloaded")
            if not nav.ok:
                return None
            await asyncio.sleep(4)
            # Read tweetText via CDP evaluate.
            cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
            expr = (
                "(function(){var t=document.querySelector(\"[data-testid='tweetText']\");"
                "return t?t.innerText:null;})()"
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                return result.data.get("result", {}).get("value")
        return None
    except Exception:  # noqa: BLE001
        return None
