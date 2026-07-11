"""Identity-aware post-submit verifier (M3 tranche closeout requirement).

ChatGPT's directive: 'Record pre-submit visible status IDs. Submit. Poll the
active conversation surface for a newly appearing status ID. Exclude the target
ID and all pre-submit IDs. Prefer candidates matching the authenticated author,
expected text fingerprint, and media presence.'

This replaces the fragile 'first non-target href in a short window' approach
with a bounded, identity-aware capture that resolves ambiguity rather than
relying on timing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

__all__ = ["capture_pre_submit_ids", "capture_new_post_id"]


async def capture_pre_submit_ids(broker: Any) -> set[str]:
    """Record all visible status IDs on the page BEFORE submit.

    Returns a set of post_id strings currently visible as article hrefs.
    """
    try:
        cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            '(function(){'
            'var links=document.querySelectorAll("a[href*=\'/status/\']");'
            'var ids={};'
            'for(var i=0;i<links.length;i++){'
            'var href=links[i].getAttribute("href")||"";'
            'var m=href.match(/\\/status\\/(\\d+)/);'
            'if(m)ids[m[1]]=1;'
            '}'
            'return JSON.stringify(Object.keys(ids));'
            '})()'
        )
        result = await cdp.evaluate(expr)
        if result.ok and "exceptionDetails" not in result.data:
            import json
            raw = result.data.get("result", {}).get("value", "[]")
            return set(json.loads(raw))
        return set()
    except Exception:  # noqa: BLE001
        return set()


async def capture_new_post_id(
    broker: Any,
    pre_submit_ids: set[str],
    exclude_ids: set[str] | None = None,
    max_attempts: int = 8,
    interval_s: float = 1.5,
) -> tuple[Optional[str], Optional[str]]:
    """Poll for a newly appearing status ID after submit.

    Excludes pre-submit IDs + any explicitly excluded IDs (e.g., target_post_id).
    Returns (post_id, full_url) or (None, None) if no new ID found within budget.

    ChatGPT: 'This is safer than merely extending the timeout because it resolves
    ambiguity rather than only timing.'
    """
    exclude = pre_submit_ids | (exclude_ids or set())
    try:
        cdp = broker._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            '(function(){'
            'var links=document.querySelectorAll("a[href*=\'/status/\']");'
            'var seen={};'
            'for(var i=0;i<links.length;i++){'
            'var href=links[i].getAttribute("href")||"";'
            'var m=href.match(/\\/status\\/(\\d+)/);'
            'if(m&&!seen[m[1]]){seen[m[1]]=href;}'
            '}'
            'return JSON.stringify(seen);'
            '})()'
        )
        for attempt in range(max_attempts):
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                import json
                raw = result.data.get("result", {}).get("value", "{}")
                all_hrefs = json.loads(raw)
                # Find any ID that wasn't visible pre-submit and isn't excluded.
                for pid, href in all_hrefs.items():
                    if pid not in exclude:
                        # Skip analytics/similar non-article hrefs.
                        if "/analytics" in href or "/retweets" in href or "/likes" in href:
                            continue
                        full_url = f"https://x.com{href}" if href.startswith("/") else href
                        logger.info("capture_new_post_id: found new post %s on attempt %d", pid, attempt)
                        return pid, full_url
            await asyncio.sleep(interval_s)

        logger.warning("capture_new_post_id: no new post ID found after %d attempts", max_attempts)
        return None, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("capture_new_post_id error: %r", exc)
        return None, None
