"""read capability — the golden read: resolve a single post from its URL.

Phase 1 per the converged contract (review conversation 6a4e7000):
- Navigate to <post_url>, hydrate, resolve the canonical top-level post.
- Required for SUCCESS: post container + post_id + author_handle + created_at.
- Best-effort: text (query_text on tweetText), metrics (parsed ints),
  display_name, lang, one-level shallow quoted_post.
- Failure: no post container, URL doesn't resolve, deleted/private/auth failure.

Identity-resolution invariant (carried from Phase 0a): ok=True is not enough —
the resolved values are verified, not assumed.
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
from webwire.capabilities.metrics import parse_metric
from webwire.envelope import (
    ActionResult,
    auth_required,
    ok_result,
    soft_failure,
)

logger = logging.getLogger(__name__)

__all__ = ["ReadCapability", "Post", "Metrics"]


@dataclass
class Metrics:
    reply_count: Optional[int] = None
    repost_count: Optional[int] = None
    like_count: Optional[int] = None
    bookmark_count: Optional[int] = None
    # Raw labels preserved for diagnostics (review Q3: keep raw evidence).
    raw: dict[str, str] = field(default_factory=dict)


@dataclass
class Post:
    post_id: Optional[str] = None
    url: Optional[str] = None
    author_handle: Optional[str] = None
    author_display_name: Optional[str] = None
    created_at: Optional[str] = None
    text: Optional[str] = None
    lang: Optional[str] = None
    metrics: Metrics = field(default_factory=Metrics)
    quoted_post: Optional["Post"] = None
    in_reply_to: Optional[str] = None  # deferred to Phase 1b/2
    # Phase 2 fan-out provenance (review Q2): when a post is read via fan-out,
    # these fields record its feed context. retweeted_by is INFERRED from
    # author != profile_handle — not an absolute semantic fact about X's
    # internal repost state.
    appeared_on_profile: Optional[str] = None
    retweeted_by: Optional[str] = None
    retweeted_by_inferred: bool = False
    # Phase 2b thread relationship: "target" | "ancestor" | "reply" | "quoted" | None.
    relationship: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


class ReadCapability:
    """Resolve a single post from its URL."""

    name = "read"
    tier = CapabilityTier.READ

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        post_url = input.get("post_url") or input.get("url")
        if not post_url:
            return soft_failure(
                "read requires 'post_url' (or 'url') input.",
                failure_category=FailureCategory.VALIDATION,
            )

        # 1. Navigate.
        nav = await broker.navigate(post_url)
        if not nav.ok:
            return nav
        await asyncio.sleep(4)  # React SPA hydration

        # 2. Observe — for login-wall detection + engagement metrics + author.
        obs = await broker.observe()
        if not obs.ok:
            return obs
        obs_data = obs.data or {}
        url = obs_data.get("url", "") or ""
        title = obs_data.get("title", "") or ""

        if _looks_like_login_wall(url, title):
            return auth_required(f"Login wall at {url!r}. Re-acquire session.")

        # 3. Post container check — article[role=article] must exist.
        container = await broker.query_attr("article", "role")
        article_found = container.ok and container.data and container.data.get("value") == "article"
        if not article_found:
            # No post container. Could be deleted/private/nonexistent.
            return soft_failure(
                f"No post container (article) found at {post_url!r}. "
                "Post may be deleted, private, or the URL is invalid.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        # 4. Gather fields.
        post = Post(url=url)

        # post_id + author_handle from the canonical /<handle>/status/<id> href.
        href_r = await broker.query_attr("a[href*='/status/']", "href")
        if href_r.ok and href_r.data:
            href = href_r.data.get("value") or ""
            post_id, author_handle = _parse_status_href(href)
            post.post_id = post_id
            post.author_handle = author_handle

        # created_at from time[datetime].
        time_r = await broker.query_attr("article time", "datetime")
        if time_r.ok and time_r.data:
            post.created_at = time_r.data.get("value")

        # text from tweetText innerText (primary) -> title (fallback).
        text_r = await broker.query_text("[data-testid='tweetText']")
        if text_r.ok and text_r.data:
            post.text = text_r.data.get("value") or None
        if not post.text:
            post.text = _text_from_title(title)

        # lang from tweetText lang attr.
        lang_r = await broker.query_attr("[data-testid='tweetText']", "lang")
        if lang_r.ok and lang_r.data:
            post.lang = lang_r.data.get("value") or None

        # display_name — best-effort, scoped to the post's User-Name area.
        # X marks the author name region with data-testid='User-Name'; the
        # display name is a span inside it. Kept optional (not required for
        # success). The earlier unscoped 'a[href] span' grabbed nav text.
        dn_r = await broker.query_text(
            "[data-testid='User-Name'] span"
        )
        if dn_r.ok and dn_r.data:
            dn = (dn_r.data.get("value") or "").strip()
            # Reject handles (@...) and known nav/non-name strings.
            if dn and not dn.startswith("@") and len(dn) > 1:
                post.author_display_name = dn

        # metrics — parsed from AX button names (engagement bar).
        post.metrics = await _read_metrics(broker, obs_data)

        # quoted_post — shallow one-level resolution (review Q2).
        # Opportunistic: a quote-tweet's value IS the quoted post, but failure
        # to resolve it must NOT fail the parent read. Returns quoted_post=None
        # if absent or unresolvable.
        post.quoted_post = await _try_read_quoted_post(broker)

        # 5. SUCCESS/PARTIAL/FAILURE per review Q4.
        required_ok = all([post.post_id, post.author_handle, post.created_at])
        if not required_ok:
            return soft_failure(
                f"read_partial: post container found but required fields missing "
                f"(post_id={post.post_id!r}, author_handle={post.author_handle!r}, "
                f"created_at={post.created_at!r}). Likely DOM churn.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        return ok_result(data=post.to_dict(), success_category=SuccessCategory.INSPECTION)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOGIN_WALL_URL = ("/login", "/i/flow/login", "oauth/authorize")
_LOGIN_WALL_TITLE = ("log in", "sign in")


def _looks_like_login_wall(url: str, title: str) -> bool:
    u, t = url.lower(), title.lower()
    return any(f in u for f in _LOGIN_WALL_URL) or any(f in t for f in _LOGIN_WALL_TITLE)


_STATUS_HREF_RE = re.compile(r"/([A-Za-z0-9_]+)/status/(\d+)")


def _parse_status_href(href: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (post_id, author_handle) from a /<handle>/status/<id> href."""
    m = _STATUS_HREF_RE.search(href or "")
    if not m:
        return None, None
    return m.group(2), m.group(1)


async def _try_read_quoted_post(broker: ReadOnlyBroker) -> Optional[Post]:
    """Resolve a shallow (one-level) quoted post if a quote-tweet card is present.

    X nests the quoted post inside [data-testid='quoteTweet'] as its own
    <article> with tweetText, a status href, and a time. Selectors are scoped
    to the quote container so they don't match the parent post.

    Opportunistic (review Q2): returns None if no quote card is present OR if
    the quote can't be resolved. Never raises — quote failure must not fail the
    parent read.
    """
    try:
        # Detect the quote-tweet container.
        qt = await broker.query_attr("[data-testid='quoteTweet']", "role")
        if not qt.ok or not qt.data or qt.data.get("value") is None:
            return None  # no quote card

        # Scope reads to the quote container.
        QT = "[data-testid='quoteTweet'] "
        quoted = Post()

        href_r = await broker.query_attr(f"{QT}a[href*='/status/']", "href")
        if href_r.ok and href_r.data:
            qid, qhandle = _parse_status_href(href_r.data.get("value") or "")
            quoted.post_id = qid
            quoted.author_handle = qhandle
            quoted.url = href_r.data.get("value")

        time_r = await broker.query_attr(f"{QT}time", "datetime")
        if time_r.ok and time_r.data:
            quoted.created_at = time_r.data.get("value")

        text_r = await broker.query_text(f"{QT}[data-testid='tweetText']")
        if text_r.ok and text_r.data:
            quoted.text = text_r.data.get("value") or None

        # A quote is only useful if it has at least an identity (post_id or
        # author). If we got nothing meaningful, return None rather than an
        # empty partial — the parent read shouldn't carry a hollow quote.
        if not (quoted.post_id or quoted.author_handle or quoted.text):
            return None
        return quoted
    except Exception as exc:  # noqa: BLE001
        logger.warning("quoted_post resolution failed: %r", exc)
        return None


def _text_from_title(title: str) -> Optional[str]:
    """Fallback: extract post text from '<author> on X: "<text>" / X'."""
    # X title format: 'jack on X: "just setting up my twttr" / X'
    m = re.match(r'^.*? on X: "(.*)"\s*/\s*X$', title)
    if m:
        return m.group(1)
    return None


async def _read_metrics(broker: ReadOnlyBroker, obs_data: dict[str, Any]) -> Metrics:
    """Parse engagement metrics from the AX snapshot button names.

    X renders: '17944 Replies. Reply', '131706 reposts. Repost',
    '308416 Likes. Like', '21252 Bookmarks. Bookmark'.
    """
    targets = obs_data.get("targets") or []
    metrics = Metrics()
    for t in targets:
        if t.get("role") != "button":
            continue
        name = (t.get("name") or "").lower()
        raw_label = t.get("name") or ""
        val = parse_metric(raw_label)
        if val is None:
            continue
        if "repl" in name:
            metrics.reply_count = val
            metrics.raw["reply"] = raw_label
        elif "repost" in name:
            metrics.repost_count = val
            metrics.raw["repost"] = raw_label
        elif "like" in name:
            metrics.like_count = val
            metrics.raw["like"] = raw_label
        elif "bookmark" in name:
            metrics.bookmark_count = val
            metrics.raw["bookmark"] = raw_label
    return metrics
