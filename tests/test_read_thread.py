"""Tests for Phase 2b read_thread helpers — post_id extraction, article
conversion, coverage semantics. Pure logic; the live scroll loop is covered
by the live smoke.
"""

from __future__ import annotations

from webwire.capabilities.read_thread import (
    ThreadCoverage,
    _article_to_thread_post,
    _extract_post_id,
    _looks_like_login_wall,
)


# -- post_id extraction ----------------------------------------------------

def test_extract_post_id_standard_url() -> None:
    assert _extract_post_id("https://x.com/jack/status/20") == "20"


def test_extract_post_id_path_only() -> None:
    assert _extract_post_id("/jack/status/12345") == "12345"


def test_extract_post_id_with_query() -> None:
    assert _extract_post_id("https://x.com/jack/status/20?s=20&t=abc") == "20"


def test_extract_post_id_none_on_no_match() -> None:
    assert _extract_post_id("https://x.com/jack") is None
    assert _extract_post_id("") is None
    assert _extract_post_id("https://x.com/home") is None


# -- article → post conversion --------------------------------------------

def test_article_to_post_standard() -> None:
    raw = {"href": "/jack/status/20", "created_at": "2006-03-21T20:50:14.000Z", "text": "hello", "lang": "en"}
    p = _article_to_thread_post(raw)
    assert p is not None
    assert p.post_id == "20"
    assert p.author_handle == "jack"
    assert p.relationship is None  # assigned later by the capability


def test_article_to_post_none_on_no_href() -> None:
    assert _article_to_thread_post({"href": None}) is None
    assert _article_to_thread_post({"href": "/jack"}) is None


# -- coverage semantics ---------------------------------------------------

def test_coverage_defaults() -> None:
    c = ThreadCoverage(limit=20)
    assert c.mode == "visible_thread_slice"
    assert c.complete is False  # always False in Phase 2b
    assert c.pagination_exhausted is False
    assert c.replies_total is None  # engagement metric ≠ crawl bound
    assert c.stop_reason == "limit_reached"


def test_coverage_stop_reasons() -> None:
    c = ThreadCoverage(limit=20)
    c.stop_reason = "pagination_exhausted"
    assert c.stop_reason == "pagination_exhausted"
    c.stop_reason = "target_not_found"
    assert c.stop_reason == "target_not_found"


# -- login wall (reused) --------------------------------------------------

def test_login_wall_detected() -> None:
    assert _looks_like_login_wall("https://x.com/i/flow/login", "Log in") is True


def test_login_wall_not_detected_for_thread() -> None:
    assert _looks_like_login_wall("https://x.com/jack/status/20", 'jack on X: "..." / X') is False
