"""read_search capability — search X for posts matching a query (Phase 2c).

Structurally similar to read_profile (flat article list + scroll), but:
- Input is a search query (not a handle).
- Tab selection via the f= URL param (top/live/user/media/list).
- Results are ranked/normalized by X — coverage must be honest about this
  (ChatGPT flagged search as "more prone to noisy result normalization").

Reuses enumerate_posts + scroll from the broker. Same guardrails as read_profile:
dedupe by post_id, stop after 2 consecutive zero-growth scrolls, hard cap,
kill-switch before each scroll.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Optional
from urllib.parse import quote_plus

from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.broker import ReadOnlyBroker
from webwire.capabilities.base import CapabilityTier
from webwire.capabilities.read import Post, _parse_status_href
from webwire.envelope import ActionResult, auth_required, ok_result, soft_failure

logger = logging.getLogger(__name__)

__all__ = ["ReadSearchCapability", "SearchCoverage"]

_SCROLL_PAUSE_S = 2.0
_MAX_SCROLL_ITERATIONS = 10
_NO_PROGRESS_THRESHOLD = 2

# Search tab → URL f= param. Per the probe: top/live/user/media/list.
_TAB_FILTERS = {
    "top": "",
    "latest": "live",
    "people": "user",
    "media": "media",
    "lists": "list",
}


@dataclass
class SearchCoverage:
    """Honest coverage metadata for search results."""
    mode: str = "visible_search_slice"
    complete: bool = False  # never claim completeness for ranked search
    pagination_exhausted: bool = False
    results_seen: int = 0
    limit: int = 20
    stop_reason: str = "limit_reached"


class ReadSearchCapability:
    """Search X for posts matching a query."""

    name = "read_search"
    tier = CapabilityTier.READ

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        query = input.get("query") or input.get("q")
        if not query:
            return soft_failure(
                "read_search requires 'query' input.",
                failure_category=FailureCategory.VALIDATION,
            )
        tab = input.get("tab", "top")
        if tab not in _TAB_FILTERS:
            return soft_failure(
                f"read_search tab must be one of {list(_TAB_FILTERS)}, got {tab!r}",
                failure_category=FailureCategory.VALIDATION,
            )
        limit = int(input.get("limit", 20))

        # 1. Build the search URL.
        f_param = _TAB_FILTERS[tab]
        search_url = f"https://x.com/search?q={quote_plus(query)}&src=typed_query"
        if f_param:
            search_url += f"&f={f_param}"

        # 2. Navigate.
        nav = await broker.navigate(search_url)
        if not nav.ok:
            return nav
        await asyncio.sleep(5)  # hydrate

        # 3. Login-wall detection.
        obs = await broker.observe()
        obs_data = obs.data or {} if obs.ok else {}
        url = obs_data.get("url", "") or ""
        title = obs_data.get("title", "") or ""
        if _looks_like_login_wall(url, title):
            return auth_required(f"Login wall at {url!r}.")

        # 4. Scroll-enumerate loop (same guardrails as read_profile/read_thread).
        posts_by_id: dict[str, Post] = {}
        scrolls = 0
        no_progress = 0
        coverage = SearchCoverage(limit=limit)

        while len(posts_by_id) < limit and scrolls < _MAX_SCROLL_ITERATIONS:
            enum_r = await broker.enumerate_posts()
            if not enum_r.ok:
                coverage.stop_reason = "parse_failed" if posts_by_id else "navigation_failed"
                break
            raw_posts = (enum_r.data or {}).get("posts", []) if enum_r.data else []
            new_count = 0
            for rp in raw_posts:
                post = _article_to_search_post(rp)
                if post and post.post_id and post.post_id not in posts_by_id:
                    posts_by_id[post.post_id] = post
                    new_count += 1
            if new_count == 0:
                no_progress += 1
                if no_progress >= _NO_PROGRESS_THRESHOLD:
                    coverage.pagination_exhausted = True
                    coverage.stop_reason = "pagination_exhausted"
                    break
            else:
                no_progress = 0
            if len(posts_by_id) >= limit:
                break
            scrolls += 1
            await broker.scroll(3000)
            await asyncio.sleep(_SCROLL_PAUSE_S)

        posts_list = list(posts_by_id.values())[:limit]
        coverage.results_seen = len(posts_list)

        result_data = {
            "query": query,
            "tab": tab,
            "source_url": search_url,
            "results": [asdict(p) for p in posts_list],
            "count": len(posts_list),
            "coverage": asdict(coverage),
        }
        return ok_result(data=result_data, success_category=SuccessCategory.INSPECTION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOGIN_WALL_URL = ("/login", "/i/flow/login", "oauth/authorize")
_LOGIN_WALL_TITLE = ("log in", "sign in")


def _looks_like_login_wall(url: str, title: str) -> bool:
    u, t = url.lower(), title.lower()
    return any(f in u for f in _LOGIN_WALL_URL) or any(f in t for f in _LOGIN_WALL_TITLE)


def _article_to_search_post(raw: dict) -> Optional[Post]:
    """Convert one search result article to a Post."""
    href = raw.get("href") or ""
    pid, author = _parse_status_href(href)
    if not pid:
        return None
    return Post(
        post_id=pid,
        url=f"https://x.com{href}" if href.startswith("/") else href,
        author_handle=author,
        created_at=raw.get("created_at"),
        text=raw.get("text"),
        lang=raw.get("lang"),
    )
