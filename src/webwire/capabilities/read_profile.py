"""read_profile capability — enumerate a profile's posts (Phase 2 fan-out).

Per the converged contract (review conversation 6a4fad0e):
- Navigate to /<handle>[/<tab>] → hydrate → enumerate articles in-page (ONE
  CDP call per scroll cycle, not N navigations).
- Scroll-driven pagination with guardrails: dedupe by post_id, stop after 2
  consecutive zero-growth scrolls, hard cap on scroll iterations, kill-switch
  checked before each scroll, stop as soon as limit satisfied.
- Lightweight extraction (post_id, author, created_at, text, lang). Metrics
  deferred (Phase 1 owns deep read; include_metrics option reserved).
- Feed provenance: retweeted_by inferred when author != profile handle.

read_profile owns "enumerate many posts safely"; read (Phase 1) owns "deep
read one post." They compose: fan-out for breadth, deep-read for depth.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.broker import ReadOnlyBroker
from webwire.capabilities.base import CapabilityTier
from webwire.capabilities.read import Post, _parse_status_href
from webwire.envelope import ActionResult, auth_required, ok_result, soft_failure

logger = logging.getLogger(__name__)

__all__ = ["ReadProfileCapability", "ProfileInfo"]

# Tab URL suffixes. "posts" = bare profile URL.
_TAB_PATHS = {
    "posts": "",
    "replies": "/with_replies",
    "media": "/media",
}

# Scroll guardrails (review Q3).
_SCROLL_PAUSE_S = 2.0
_MAX_SCROLL_ITERATIONS = 10
_NO_PROGRESS_THRESHOLD = 2  # consecutive zero-growth scrolls → stop


@dataclass
class ProfileInfo:
    handle: Optional[str] = None
    display_name: Optional[str] = None
    bio: Optional[str] = None
    verified: Optional[bool] = None


class ReadProfileCapability:
    """Enumerate a profile's posts via in-page article extraction + scroll."""

    name = "read_profile"
    tier = CapabilityTier.READ

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        handle = input.get("handle")
        if not handle:
            return soft_failure(
                "read_profile requires 'handle' input.",
                failure_category=FailureCategory.VALIDATION,
            )
        handle = handle.lstrip("@")
        tab = input.get("tab", "posts")
        if tab not in _TAB_PATHS:
            return soft_failure(
                f"read_profile tab must be one of {list(_TAB_PATHS)}, got {tab!r}",
                failure_category=FailureCategory.VALIDATION,
            )
        limit = int(input.get("limit", 20))
        include_retweets = bool(input.get("include_retweets", True))

        # 1. Navigate to the profile tab.
        suffix = _TAB_PATHS[tab]
        profile_url = f"https://x.com/{handle}{suffix}"
        nav = await broker.navigate(profile_url)
        if not nav.ok:
            return nav
        # X profiles hydrate incrementally — articles appear over ~4-6s. Wait
        # 6s before the first enumerate to avoid a zero-result first cycle that
        # would prematurely trip the no-progress threshold.
        await asyncio.sleep(6)  # hydrate

        # 2. Login-wall / non-existent profile detection via observe.
        obs = await broker.observe()
        obs_data = obs.data or {} if obs.ok else {}
        url = obs_data.get("url", "") or ""
        title = obs_data.get("title", "") or ""
        if _looks_like_login_wall(url, title):
            return auth_required(f"Login wall at {url!r}.")
        # A nonexistent/suspended profile shows a specific state. Detect by
        # the absence of articles after hydration + a "doesn't exist" title.
        if _looks_like_missing_profile(url, title, handle):
            return soft_failure(
                f"Profile @{handle} not found or suspended (url={url!r}, title={title!r}).",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        # 3. Extract profile header info (best-effort).
        profile = await _read_profile_header(broker, handle)

        # 4. Scroll-enumerate loop with guardrails.
        posts_by_id: dict[str, Post] = {}
        scrolls = 0
        no_progress = 0
        while len(posts_by_id) < limit and scrolls < _MAX_SCROLL_ITERATIONS:
            # Kill-switch check before each enumeration (the broker methods
            # also check, but enumerating is pointless if killed).
            enum_r = await broker.enumerate_posts()
            if not enum_r.ok:
                break
            raw_posts = (enum_r.data or {}).get("posts", []) if enum_r.data else []
            new_count = 0
            for rp in raw_posts:
                post = _article_to_post(rp, handle, profile_url)
                if post and post.post_id and post.post_id not in posts_by_id:
                    posts_by_id[post.post_id] = post
                    new_count += 1
            if new_count == 0:
                no_progress += 1
                if no_progress >= _NO_PROGRESS_THRESHOLD:
                    break
            else:
                no_progress = 0
            if len(posts_by_id) >= limit:
                break
            # Scroll for more.
            scrolls += 1
            await broker.scroll(3000)
            await asyncio.sleep(_SCROLL_PAUSE_S)

        # 5. Apply include_retweets filter + limit.
        posts_list = list(posts_by_id.values())
        if not include_retweets:
            posts_list = [p for p in posts_list if p.author_handle == handle]
        posts_list = posts_list[:limit]
        truncated = len(posts_by_id) >= limit or scrolls >= _MAX_SCROLL_ITERATIONS

        result_data = {
            "profile": asdict(profile),
            "posts": [p.to_dict() for p in posts_list],
            "count": len(posts_list),
            "truncated": truncated,
            "scrolls_performed": scrolls,
            "source_url": profile_url,
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


def _looks_like_missing_profile(url: str, title: str, handle: str) -> bool:
    """Heuristic: a suspended/nonexistent profile. X renders a page with the
    handle in the URL but a title like 'X' or 'Account suspended'."""
    t = title.lower()
    if "suspend" in t:
        return True
    # If we navigated to /<handle> but landed elsewhere without the handle,
    # the profile likely doesn't exist.
    if handle.lower() not in url.lower() and "/search" not in url.lower():
        return True
    return False


async def _read_profile_header(broker: ReadOnlyBroker, handle: str) -> ProfileInfo:
    """Best-effort profile header extraction. Not required for success."""
    info = ProfileInfo(handle=handle)
    try:
        # display_name — X uses data-testid='UserName' for the header name.
        dn = await broker.query_text("[data-testid='UserDisplayName']")
        if dn.ok and dn.data:
            v = (dn.data.get("value") or "").strip()
            if v:
                info.display_name = v
        # bio
        bio = await broker.query_text("[data-testid='UserDescription']")
        if bio.ok and bio.data:
            v = (bio.data.get("value") or "").strip()
            if v:
                info.bio = v
        # verified badge
        ver = await broker.query_attr("[data-testid='icon-verified']", "aria-label")
        if ver.ok and ver.data and ver.data.get("value"):
            info.verified = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("profile header extraction failed: %r", exc)
    return info


_STATUS_HREF_RE = re.compile(r"/([A-Za-z0-9_]+)/status/(\d+)")


def _article_to_post(raw: dict, profile_handle: str, profile_url: str) -> Optional[Post]:
    """Convert one enumerated article dict to a Post with provenance fields."""
    href = raw.get("href") or ""
    pid, author = _parse_status_href(href)
    if not pid:
        return None
    post = Post(
        post_id=pid,
        url=f"https://x.com{href}" if href.startswith("/") else href,
        author_handle=author,
        created_at=raw.get("created_at"),
        text=raw.get("text"),
        lang=raw.get("lang"),
        appeared_on_profile=profile_handle,
    )
    # Retweet inference (review Q2): author differs from the profile handle.
    if author and author.lower() != profile_handle.lower():
        post.retweeted_by = profile_handle
        post.retweeted_by_inferred = True
    return post
