"""Tests for Phase 2 read_profile helpers — article-to-post conversion,
retweet inference, missing-profile detection. Pure logic; the live scroll
loop is covered by the live smoke.
"""

from __future__ import annotations

from webwire.capabilities.read_profile import (
    _article_to_post,
    _looks_like_login_wall,
    _looks_like_missing_profile,
)

# -- article → post conversion + retweet inference -------------------------

def test_article_self_post_no_retweet() -> None:
    raw = {"href": "/jack/status/20", "created_at": "2006-03-21T20:50:14.000Z", "text": "hello", "lang": "en"}
    p = _article_to_post(raw, "jack", "https://x.com/jack")
    assert p is not None
    assert p.post_id == "20"
    assert p.author_handle == "jack"
    assert p.retweeted_by is None
    assert p.retweeted_by_inferred is False
    assert p.appeared_on_profile == "jack"
    assert p.url == "https://x.com/jack/status/20"


def test_article_retweet_inferred() -> None:
    """A post on jack's profile authored by someone else → retweeted_by=jack."""
    raw = {"href": "/spiral_xyz/status/2075022121244254464", "created_at": "2026-07-09T01:00:00.000Z", "text": "lots to see", "lang": "en"}
    p = _article_to_post(raw, "jack", "https://x.com/jack")
    assert p is not None
    assert p.author_handle == "spiral_xyz"
    assert p.retweeted_by == "jack"
    assert p.retweeted_by_inferred is True


def test_article_case_insensitive_handle_match() -> None:
    """Jack vs jack — same handle, different case → not a retweet."""
    raw = {"href": "/Jack/status/20", "created_at": None, "text": "x", "lang": None}
    p = _article_to_post(raw, "jack", "https://x.com/jack")
    assert p is not None
    assert p.retweeted_by is None  # case-insensitive match


def test_article_no_post_id_returns_none() -> None:
    raw = {"href": "/jack", "created_at": None, "text": None, "lang": None}
    assert _article_to_post(raw, "jack", "https://x.com/jack") is None


def test_article_empty_href_returns_none() -> None:
    raw = {"href": None, "created_at": None, "text": None, "lang": None}
    assert _article_to_post(raw, "jack", "https://x.com/jack") is None


def test_article_absolute_url_href() -> None:
    raw = {"href": "https://x.com/jack/status/20", "created_at": None, "text": "hi", "lang": None}
    p = _article_to_post(raw, "jack", "https://x.com/jack")
    assert p is not None
    assert p.url == "https://x.com/jack/status/20"


# -- missing profile detection ---------------------------------------------

def test_missing_profile_suspended_title() -> None:
    assert _looks_like_missing_profile("https://x.com/baduser", "Account suspended", "baduser") is True


def test_missing_profile_handle_not_in_url() -> None:
    """Navigated to /baduser but landed elsewhere without the handle."""
    assert _looks_like_missing_profile("https://x.com/home", "X", "baduser") is True


def test_valid_profile_not_missing() -> None:
    assert _looks_like_missing_profile("https://x.com/jack", "jack (@jack) / X", "jack") is False


def test_missing_profile_search_redirect_not_missing() -> None:
    """X redirects unknown handles to /search — that's not 'missing', it's a
    known surface; don't false-positive."""
    assert _looks_like_missing_profile("https://x.com/search?q=baduser", "Explore", "baduser") is False


# -- login wall (reused from read, but tested here too) --------------------

def test_login_wall_detected() -> None:
    assert _looks_like_login_wall("https://x.com/i/flow/login", "Log in") is True


def test_login_wall_not_detected_for_profile() -> None:
    assert _looks_like_login_wall("https://x.com/jack", "jack (@jack) / X") is False
