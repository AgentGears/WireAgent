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


def _post_id_from_url(url: str) -> Optional[str]:
    """Extract the status id from a post permalink (used to scope article
    selection — on a REPLY permalink the first article is the PARENT post,
    so first-article scoping counts/reads the wrong post)."""
    m = re.search(r"/status/(\d+)", url or "")
    return m.group(1) if m else None


# Post-navigation render wait: X hydrates client-side; a fixed sleep races it
# (caught live during M4b verification — a 4s sleep evaluated a blank page).
# Poll until the scoped answer exists or the deadline passes.
_RENDER_TIMEOUT_S = 8.0
_RENDER_POLL_INTERVAL_S = 1.0


async def _poll_until_present(factory) -> Optional[Any]:
    """Poll factory() until it returns a non-None answer or deadline.
    factory returns None while the page hasn't rendered the answer."""
    import time
    deadline = time.monotonic() + _RENDER_TIMEOUT_S
    while True:
        value = await factory()
        if value is not None:
            return value
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(_RENDER_POLL_INTERVAL_S)


async def verify_post_text(broker: Any, posted_url: str, normalized: str) -> bool:
    """Navigate to the posted URL and compare ITS article's tweetText
    (whitespace-collapsed) against the submitted text. The article is scoped
    by the posted status id — not the first article, which on a reply
    permalink is the parent. Polls until the article renders or deadline.
    False on any failure."""
    try:
        post_id = _post_id_from_url(posted_url)
        if post_id is None or not hasattr(broker, "_sb"):
            return False
        nav = await broker._sb.navigate(posted_url, wait_until="domcontentloaded")
        if not nav.ok:
            return False
        cdp = broker._sb._controller._cdp
        expr = (
            '(function(){'
            f'var arts=document.querySelectorAll("article");'
            f'for(var i=0;i<arts.length;i++){{'
            f'var link=arts[i].querySelector("a[href*=\'/status/{post_id}\']");'
            f'if(!link)continue;'
            f'var t=arts[i].querySelector("[data-testid=\'tweetText\']");'
            f'return t?t.innerText:null;}}'
            f'return null;}})()'
        )

        async def _read() -> Optional[str]:
            r = await cdp.evaluate(expr)
            if not r.ok or "exceptionDetails" in r.data:
                return None
            return r.data.get("result", {}).get("value")

        # None while the page hasn't rendered the article; empty string only
        # once the element exists (definitive — a text post can be empty-text).
        text = await _poll_until_present(_read)
        if text is None:
            return False
        return normalize_for_compare(text) == normalize_for_compare(normalized)
    except Exception:  # noqa: BLE001
        return False


async def count_post_media(broker: Any, posted_url: str) -> int:
    """Count media owned by the exact posted status's direct article.

    The article is selected only by a direct timestamp-owned status link. Media
    nested under quoted-content subtrees or nested articles is not evidence for
    the outer post. Poll until owned photos render or the deadline passes; zero
    remains unverified so callers fail closed rather than infer attachment truth.
    """
    try:
        post_id = _post_id_from_url(posted_url)
        if post_id is None or not hasattr(broker, "_sb"):
            return 0
        nav = await broker._sb.navigate(posted_url, wait_until="domcontentloaded")
        if not nav.ok:
            return 0
        cdp = broker._sb._controller._cdp
        expr = (
            "(function(){"
            f"var target='{post_id}';"
            'var arts=document.querySelectorAll("article");'
            "for(var i=0;i<arts.length;i++){var art=arts[i],owns=false;"
            "var links=art.querySelectorAll('a[href]');"
            "for(var j=0;j<links.length;j++){var a=links[j];"
            "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
            "try{var u=new URL(a.href,location.href);"
            "var m=u.pathname.match(/\\/status\\/(\\d+)(?:\\/|$)/);"
            "if(m&&m[1]===target){owns=true;break;}}catch(e){}}"
            "if(!owns)continue;"
            'var photos=art.querySelectorAll("[data-testid=\'tweetPhoto\']"),count=0;'
            "for(var p=0;p<photos.length;p++){var photo=photos[p];"
            "if(photo.closest('article')!==art)continue;"
            'var quote=photo.closest("[data-testid=\'quoteTweet\']");'
            "if(quote&&art.contains(quote))continue;count++;}"
            "return count;}return null;})()"
        )

        async def _read() -> Optional[int]:
            r = await cdp.evaluate(expr)
            if not r.ok or "exceptionDetails" in r.data:
                return None
            value = r.data.get("result", {}).get("value")
            if value is None:
                return None        # article not rendered yet — keep polling
            if value == 0:
                return None        # owned photos may still be loading
            return int(value)

        count = await _poll_until_present(_read)
        return int(count) if count is not None else 0
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
        cdp = broker._sb._controller._cdp
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
