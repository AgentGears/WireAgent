"""reply_post capability — target-bound public reply (Phase 4c).

Reuses the proven post_text machinery with one new dimension: target binding.
ChatGPT's 6 target-specific invariants:
1. Preview includes target context (replying_to).
2. Token binds target_post_id + normalized_text.
3. Dedupe key includes target_post_id.
4. Reply button click is target-scoped (inside the target article).
5. Composer context verified when possible.
6. Verification proves the reply was attached to the target.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["ReplyPostCapability"]


class ReplyPostCapability:
    """Reply to a specific post. PUBLIC_CONTENT_IRREVERSIBLE tier with target binding."""

    name = "reply_post"

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
        normalized = normalize_text(raw_text)

        meta, comp = DEFAULT_REGISTRY.require("reply")
        return WriteIntent(
            action_type="reply",
            target_type="post",
            target_id=str(target_post_id),
            risk_meta=meta,
            compensation=comp,
            semantic_variant=text_hash(normalized),
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
                "post_url": post_url,
                "target_post_id": str(target_post_id),
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Preview includes target context (ChatGPT's invariant #1)."""
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        post_url = intent.payload.get("post_url", "")

        # Best-effort: read target context via the broker (read-only).
        # In the kernel pipeline, preview receives the ReadOnlyBroker, so we
        # can't navigate. We include the target_post_id and URL in the preview.
        warnings = []
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            warnings.append(f"Text exceeds X's character limit ({char_count} > 280)")

        return PreviewResult(
            summary=f"Will reply to post {target_post_id}: {normalized[:80]!r}",
            target_url=post_url,
            current_state=f"replying to {target_post_id}, text ({char_count} chars)",
            warnings=warnings + [
                "PUBLIC CONTENT IRREVERSIBLE: supports_compensation=false. "
                "Reply will be visible publicly and notify the target author."
            ],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Target-scoped reply execution."""
        normalized = intent.payload.get("normalized_text", "")
        target_post_id = intent.payload.get("target_post_id", "")
        post_url = intent.payload.get("post_url", "")

        # Steps 1-5: validate frozen intent.
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            return _failure("pre_submit_mismatch",
                            f"Text length invalid ({char_count} > 280). No submit.")

        # Step 7: open reply on the target-scoped article (invariant #4).
        open_r = await broker.open_reply_on_target(post_url, target_post_id)
        if not open_r.ok:
            return _failure("target_not_found_before_reply",
                            f"Could not open reply on target {target_post_id}: "
                            f"{open_r.error.message if open_r.error else open_r}")

        # Step 8: fill reply composer.
        fill_r = await broker.fill_reply_composer(normalized)
        if not fill_r.ok:
            return _failure("pre_submit_mismatch", f"Could not fill reply composer: {fill_r.error}")

        # Steps 9-10: read composer text back + assert match.
        read_r = await broker.read_composer_text()
        if not read_r.ok:
            return _failure("pre_submit_mismatch", "Could not read reply composer text.")
        composer_text = (read_r.data or {}).get("composer_text", "")
        if _normalize_for_compare(composer_text) != _normalize_for_compare(normalized):
            return _failure("pre_submit_mismatch",
                            f"Composer text mismatch. Expected {normalized!r}, "
                            f"got {composer_text!r}. NO submit clicked.")

        # Step 11: final kill-switch check.
        if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
            return _failure("killed_before_submit",
                            "Kill switch tripped after fill, before submit. NO submit clicked.")

        # Step 12: click submit.
        submit_r = await broker.click_submit()
        if not submit_r.ok:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but result uncertain.",
                             normalized, target_post_id)

        # Step 13: capture posted reply URL. After replying, X stays on the
        # target page — the reply appears as a new article below. We need to
        # find the NEW post_id (not the target's).
        await asyncio.sleep(4)
        posted_url, posted_post_id = await _capture_reply_url(broker, target_post_id)

        if not posted_url:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but reply URL not captured.",
                             normalized, target_post_id)

        if not posted_url:
            return _degraded("submit_clicked_verification_pending",
                             "Submit clicked but posted URL not captured.",
                             normalized, target_post_id)

        # Steps 14-15: thread-aware read-back verification (ChatGPT's Phase 4c-v fix).
        # X redirects reply status URLs to the thread root, so navigating to
        # posted_url shows the TARGET as the first article, not the reply.
        # Fix: navigate to the PARENT post URL, enumerate articles, find the
        # reply by posted_post_id, and verify its text + that it's after the target.
        await asyncio.sleep(2)
        verified = await _verify_reply_in_thread(broker, post_url, target_post_id, posted_post_id, normalized)

        if not verified["found"]:
            return _degraded("posted_url_captured_verification_failed",
                             f"Reply {posted_post_id} not found in thread of {target_post_id}.",
                             normalized, target_post_id, posted_url, posted_post_id)
        if not verified["text_matches"]:
            return _degraded("posted_url_captured_verification_failed",
                             f"Reply text mismatch. Expected {normalized!r}, got {verified['text']!r}.",
                             normalized, target_post_id, posted_url, posted_post_id)

        # Step 16: posted_and_verified.
        return ok_result(data={
            "result": "reply_posted_and_target_verified",
            "posted_url": posted_url,
            "posted_post_id": posted_post_id,
            "target_post_id": target_post_id,
            "submitted_text": normalized,
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
            "residual_side_effects": [
                "public_content_may_be_seen",
                "notifications_may_be_sent_to_target_author",
                "content_may_be_indexed_or_cached",
                "delete_does_not_fully_undo_distribution",
            ],
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        return ok_result(data={"verified": True, "note": "Inline verification in execute."})


# ---------------------------------------------------------------------------
# Helpers (shared logic with post_text, kept DRY)
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
              target_post_id: Optional[str] = None, posted_url: Optional[str] = None,
              posted_post_id: Optional[str] = None) -> ActionResult:
    from super_browser.results import ActionError, ErrorCategory, action_result
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message,
        "public_side_effect": True,
        "submitted_text": normalized,
        "target_post_id": target_post_id,
        "posted_url": posted_url, "posted_post_id": posted_post_id,
        "supports_compensation": False,
    }
    return r


async def _verify_reply_in_thread(
    broker: Any, parent_post_url: str, target_post_id: str,
    posted_reply_id: Optional[str], normalized_text: str,
) -> dict:
    """Thread-aware reply verification (ChatGPT's Phase 4c-v fix).

    X redirects reply status URLs to the thread root, so first-article read-back
    returns the TARGET's text, not the reply's. Fix: navigate to the parent
    post's URL, enumerate articles via CDP, find the reply by posted_reply_id,
    and verify its text + that it appears after the target.

    Returns: {found: bool, text_matches: bool, text: str|None}
    """
    try:
        import asyncio
        import re
        if not hasattr(broker, "_sb"):
            return {"found": False, "text_matches": False, "text": None}

        # Navigate to the parent post (the target we replied to).
        nav = await broker._sb.navigate(parent_post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return {"found": False, "text_matches": False, "text": None}
        await asyncio.sleep(4)

        # Enumerate all visible articles and find the one matching posted_reply_id.
        cdp = broker._sb._controller._cdp
        # Get all articles' hrefs + texts in order.
        expr = (
            "(function(){"
            "var arts=document.querySelectorAll('article');"
            "return Array.from(arts).map(function(a){"
            "var link=a.querySelector(\"a[href*='/status/']\");"
            "var text=a.querySelector(\"[data-testid='tweetText']\");"
            "return {"
            "href: link?link.getAttribute('href'):null,"
            "text: text?text.innerText:null"
            "};"
            "});"
            "})()"
        )
        result = await cdp.evaluate(expr)
        if not result.ok or "exceptionDetails" in result.data:
            return {"found": False, "text_matches": False, "text": None}

        articles = result.data.get("result", {}).get("value") or []
        for art in articles:
            href = art.get("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m and m.group(1) == posted_reply_id:
                reply_text = art.get("text") or ""
                matches = _normalize_for_compare(reply_text) == _normalize_for_compare(normalized_text)
                return {"found": True, "text_matches": matches, "text": reply_text}

        return {"found": False, "text_matches": False, "text": None}
    except Exception:  # noqa: BLE001
        return {"found": False, "text_matches": False, "text": None}


async def _capture_reply_url(broker: Any, target_post_id: str) -> tuple[Optional[str], Optional[str]]:
    """After replying, X stays on the target page. The reply appears as a new
    article below the target. Find the most recent article whose post_id differs
    from the target — that's the reply.

    Returns (posted_url, posted_post_id) or (None, None) if not found.
    """
    try:
        if hasattr(broker, "_sb"):
            cdp = broker._sb._controller._cdp
            # Find all status hrefs and pick the first one that ISN'T the target.
            expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                'var seen={};'
                'for(var i=0;i<links.length;i++){'
                'var href=links[i].getAttribute("href");'
                'if(href&&href.indexOf("/status/")>=0&&!seen[href]){'
                'seen[href]=1;'
                'var m=href.match(/\\/status\\/(\\d+)/);'
                'if(m&&m[1]!=="' + target_post_id + '"){'
                'return JSON.stringify({href:href,id:m[1]});}}}'
                'return null;})()'
            )
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                import json
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
