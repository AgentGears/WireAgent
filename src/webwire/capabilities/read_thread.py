"""read_thread capability — visible conversation-slice reader (Phase 2b).

Per converged design (conversation 6a4fcb79):
- Target = article whose post_id matches the input URL's status id (NOT first
  article). Articles before target = ancestors; after = replies.
- Coverage: mode="visible_thread_slice", complete=False always, pagination_exhausted
  only after 2 consecutive zero-growth scrolls. stop_reason explains why we stopped.
- replies_total = None (engagement metric ≠ crawl completeness).
- Dedupe by post_id (X may recycle/duplicate DOM nodes during scroll).
- Quotes live per-Post (quoted_post field), not top-level.
- Self-thread walking deferred.

Reuses enumerate_posts + scroll from the broker (same machinery as read_profile).
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

__all__ = ["ReadThreadCapability", "ThreadCoverage"]

_SCROLL_PAUSE_S = 2.0
_MAX_SCROLL_ITERATIONS = 10
_NO_PROGRESS_THRESHOLD = 2


@dataclass
class ThreadCoverage:
    """Honest coverage metadata. Does NOT claim thread completeness."""
    mode: str = "visible_thread_slice"
    complete: bool = False  # always False in Phase 2b
    pagination_exhausted: bool = False
    posts_seen: int = 0
    ancestors_seen: int = 0
    replies_seen: int = 0
    replies_total: Optional[int] = None  # None — engagement metric ≠ crawl bound
    limit: int = 20
    stop_reason: str = "limit_reached"  # limit_reached | pagination_exhausted | target_not_found


class ReadThreadCapability:
    """Read the visible conversation slice around a target post."""

    name = "read_thread"
    tier = CapabilityTier.READ

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        post_url = input.get("post_url") or input.get("url")
        if not post_url:
            return soft_failure(
                "read_thread requires 'post_url' input.",
                failure_category=FailureCategory.VALIDATION,
            )

        # Parse target post_id from the URL (the safety anchor).
        target_post_id = _extract_post_id(post_url)
        if not target_post_id:
            return soft_failure(
                f"Could not parse post_id from {post_url!r}. Expected /<handle>/status/<id>.",
                failure_category=FailureCategory.VALIDATION,
            )

        # 1. Navigate to the thread page.
        nav = await broker.navigate(post_url)
        if not nav.ok:
            return nav
        await asyncio.sleep(5)  # hydrate (threads are heavier than profiles)

        # 2. Login-wall detection.
        obs = await broker.observe()
        obs_data = obs.data or {} if obs.ok else {}
        url = obs_data.get("url", "") or ""
        title = obs_data.get("title", "") or ""
        if _looks_like_login_wall(url, title):
            return auth_required(f"Login wall at {url!r}.")

        # 3. Scroll-enumerate loop (same guardrails as read_profile).
        posts_by_id: dict[str, dict[str, Any]] = {}  # post_id -> raw article dict
        ordered_ids: list[str] = []  # preserve first-seen order
        scrolls = 0
        no_progress = 0
        limit = int(input.get("limit", 20))
        coverage = ThreadCoverage(limit=limit)

        while len(posts_by_id) < limit and scrolls < _MAX_SCROLL_ITERATIONS:
            enum_r = await broker.enumerate_posts()
            if not enum_r.ok:
                coverage.stop_reason = "parse_failed" if posts_by_id else "navigation_failed"
                break
            raw_posts = (enum_r.data or {}).get("posts", []) if enum_r.data else []
            new_count = 0
            for rp in raw_posts:
                pid, _ = _parse_status_href(rp.get("href") or "")
                if pid and pid not in posts_by_id:
                    posts_by_id[pid] = rp
                    ordered_ids.append(pid)
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

        # 4. Find the target by post_id (NOT positional first-article).
        target_index = None
        for i, pid in enumerate(ordered_ids):
            if pid == target_post_id:
                target_index = i
                break

        if target_index is None:
            # Target not found in the visible slice — structured failure.
            coverage.stop_reason = "target_not_found"
            return soft_failure(
                f"target_not_found_in_visible_thread: post {target_post_id!r} was not "
                f"among the {len(posts_by_id)} visible articles. It may be collapsed, "
                f"ranked lower, or deleted.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        # 5. Assign relationships: before target = ancestors, target = target,
        #    after target = replies.
        ancestors: list[Post] = []
        target_post: Optional[Post] = None
        replies: list[Post] = []

        for i, pid in enumerate(ordered_ids):
            rp = posts_by_id[pid]
            post = _article_to_thread_post(rp)
            if not post:
                continue
            if i < target_index:
                post.relationship = "ancestor"
                ancestors.append(post)
            elif i == target_index:
                post.relationship = "target"
                target_post = post
            else:
                post.relationship = "reply"
                replies.append(post)

        if target_post is None:
            coverage.stop_reason = "parse_failed"
            return soft_failure(
                f"target post {target_post_id!r} found but could not be parsed.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        # 6. Build coverage.
        coverage.posts_seen = len(posts_by_id)
        coverage.ancestors_seen = len(ancestors)
        coverage.replies_seen = len(replies)

        result_data = {
            "target_post": target_post.to_dict() if hasattr(target_post, 'to_dict') else asdict(target_post),
            "ancestors": [asdict(a) for a in ancestors],
            "replies": [asdict(r) for r in replies],
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


_STATUS_ID_RE = re.compile(r"/status/(\d+)")


def _extract_post_id(url: str) -> Optional[str]:
    """Extract the status id from a post URL or path."""
    m = _STATUS_ID_RE.search(url or "")
    return m.group(1) if m else None


def _article_to_thread_post(raw: dict) -> Optional[Post]:
    """Convert one enumerated article to a Post (without relationship — assigned later)."""
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
