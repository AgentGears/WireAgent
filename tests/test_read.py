"""Tests for Phase 1 read capability helpers — metrics parser, href parser,
title-text parser. These are the pure, deterministic pieces; the live DOM
integration is covered by the live smoke.
"""

from __future__ import annotations

from webwire.capabilities.metrics import parse_metric
from webwire.capabilities.read import _parse_status_href, _text_from_title

# -- metrics parser --------------------------------------------------------

def test_metric_full_integer() -> None:
    assert parse_metric("308416 Likes. Like") == 308416


def test_metric_comma_grouped() -> None:
    assert parse_metric("308,416 Likes. Like") == 308416


def test_metric_abbreviated_k() -> None:
    assert parse_metric("1.2K Replies. Reply") == 1200


def test_metric_abbreviated_m() -> None:
    assert parse_metric("5M reposts. Repost") == 5_000_000


def test_metric_abbreviated_b() -> None:
    assert parse_metric("2B Likes. Like") == 2_000_000_000


def test_metric_lowercase_abbrev() -> None:
    assert parse_metric("3.5k Likes. Like") == 3500


def test_metric_zero() -> None:
    assert parse_metric("0 Likes. Like") == 0


def test_metric_none_on_garbage() -> None:
    assert parse_metric("Likes. Like") is None  # no leading number


def test_metric_none_on_empty() -> None:
    assert parse_metric("") is None
    assert parse_metric(None) is None


def test_metric_bare_decimal_without_suffix_rejected() -> None:
    """A bare decimal '1.234' is ambiguous (decimal vs European grouping).
    Review Q3: never guess — reject rather than return 1 or 1234."""
    assert parse_metric("1.234 Likes. Like") is None


def test_metric_abbreviated_with_comma() -> None:
    assert parse_metric("1,200 Likes. Like") == 1200


def test_metric_bookmark_not_misread_as_billions() -> None:
    """REGRESSION: '21252 Bookmarks. Bookmark' was misparsed as 21252 * 1B
    because the 'B' in 'Bookmarks' matched the abbreviation suffix. The fix
    requires the suffix to be a standalone token (word boundary). The correct
    value is 21252, and the word 'Bookmarks' must NOT be read as a 'B' suffix."""
    assert parse_metric("21252 Bookmarks. Bookmark") == 21252
    # Also: 'reposts' must not have its 'r' or any letter misread.
    assert parse_metric("131706 reposts. Repost") == 131706


# -- status href parser ----------------------------------------------------

def test_parse_status_href_standard() -> None:
    pid, handle = _parse_status_href("/jack/status/20")
    assert pid == "20"
    assert handle == "jack"


def test_parse_status_href_absolute() -> None:
    pid, handle = _parse_status_href("https://x.com/infaag/status/1234567890")
    assert pid == "1234567890"
    assert handle == "infaag"


def test_parse_status_href_with_query() -> None:
    pid, handle = _parse_status_href("/jack/status/20?s=20")
    assert pid == "20"
    assert handle == "jack"


def test_parse_status_href_none_on_no_match() -> None:
    assert _parse_status_href("/jack") == (None, None)
    assert _parse_status_href("") == (None, None)
    assert _parse_status_href("/home") == (None, None)


# -- title text fallback parser --------------------------------------------

def test_text_from_title_standard() -> None:
    assert _text_from_title('jack on X: "just setting up my twttr" / X') == "just setting up my twttr"


def test_text_from_title_none_on_mismatch() -> None:
    assert _text_from_title("Home / X") is None
    assert _text_from_title("") is None


def test_text_from_title_empty_quotes() -> None:
    # An empty-text post wouldn't normally produce this title, but the parser
    # shouldn't crash on it.
    assert _text_from_title('jack on X: "" / X') == ""
