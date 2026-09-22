"""Post-submit media verification helpers (single home, M4b extraction 2026-09-22).

Moved verbatim from the capability modules (post_multi_image._verify_text /
_count_post_media, reply_photo._verify_reply_in_thread) so M4a and M4b share
one definition each. Capability modules that tests monkeypatch keep importing
these under their historical private names — patches in the caller's module
namespace still take effect because the capability resolves them at call time.

All helpers are honest-verifier shaped: they return evidence (found/not,
count, match/mismatch) and never claim more than the DOM can prove.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Optional


def normalize_for_compare(text: str) -> str:
    """Whitespace-collapsed comparison form (unchanged semantics from the
    four capability copies it replaces)."""
    import re as _re
    return _re.sub(r"\s+", " ", text.strip())


async def verify_post_text(broker: Any, posted_url: str, normalized: str) -> bool:
    """Navigate to the posted URL and compare its tweetText (whitespace-
    collapsed) against the submitted text. False on any failure."""
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
                return normalize_for_compare(text) == normalize_for_compare(normalized)
        return False
    except Exception:  # noqa: BLE001
        return False


async def count_post_media(broker: Any, posted_url: str) -> int:
    """Count tweetPhoto elements on the posted page (article-scoped where
    available). 0 on any failure — callers treat 0 as unverified, honestly."""
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


async def verify_reply_in_thread(
    broker: Any,
    parent_post_url: str,
    target_post_id: str,
    posted_reply_id: str,
    normalized_text: str,
) -> dict:
    """Thread-aware reply verification: navigate the PARENT post's thread,
    find the article whose status href matches the posted reply id, compare
    text. Returns {"found": bool, "text_matches": bool} — both False on any
    failure (never claims what the DOM didn't show)."""
    try:
        if not hasattr(broker, "_sb"):
            return {"found": False, "text_matches": False}
        nav = await broker._sb.navigate(parent_post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return {"found": False, "text_matches": False}
        await asyncio.sleep(4)
        cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            "(function(){"
            "var arts=document.querySelectorAll('article');"
            "return Array.from(arts).map(function(a){"
            "var link=a.querySelector(\"a[href*='/status/']\");"
            "var text=a.querySelector(\"[data-testid='tweetText']\");"
            "return {href: link?link.getAttribute('href'):null,"
            "text: text?text.innerText:null};});"
            "})()"
        )
        result = await cdp.evaluate(expr)
        if not result.ok or "exceptionDetails" in result.data:
            return {"found": False, "text_matches": False}
        articles = result.data.get("result", {}).get("value") or []
        for art in articles:
            href = art.get("href") or ""
            m = re.search(r"/status/(\d+)", href)
            if m and m.group(1) == posted_reply_id:
                reply_text = art.get("text") or ""
                matches = (
                    normalize_for_compare(reply_text)
                    == normalize_for_compare(normalized_text)
                )
                return {"found": True, "text_matches": matches}
        return {"found": False, "text_matches": False}
    except Exception:  # noqa: BLE001
        return {"found": False, "text_matches": False}


__all__ = [
    "normalize_for_compare",
    "verify_post_text",
    "count_post_media",
    "verify_reply_in_thread",
]
