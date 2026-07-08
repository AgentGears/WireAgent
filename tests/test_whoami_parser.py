"""Tests for the whoami identity parser + login-wall detection.

These test the maintenance-sensitive helpers directly — the parts most likely
to break when X's DOM changes — without needing a live browser.
"""

from __future__ import annotations

from webwire.capabilities.whoami import (
    _looks_like_login_wall,
    _parse_identity_from_observation,
)


def test_login_wall_detected_by_url() -> None:
    assert _looks_like_login_wall("https://x.com/i/flow/login", "Anything") is True
    assert _looks_like_login_wall("https://x.com/login", "Home") is True


def test_login_wall_detected_by_title() -> None:
    assert _looks_like_login_wall("https://x.com/home", "Log in to X") is True
    assert _looks_like_login_wall("https://x.com/home", "Sign in") is True


def test_login_wall_not_detected_for_legit_home() -> None:
    assert _looks_like_login_wall("https://x.com/home", "Home / X") is False
    assert _looks_like_login_wall("https://twitter.com/foo", "foo (@foo) / X") is False


def test_parse_finds_at_handle_target() -> None:
    obs = {
        "url": "https://x.com/home",
        "title": "Home / X",
        "targets": [
            {"role": "link", "name": "@testuser"},
            {"role": "button", "name": "Post"},
        ],
    }
    ident = _parse_identity_from_observation(obs)
    assert ident is not None
    assert ident["handle"] == "testuser"
    assert ident["profile_url"] == "https://x.com/testuser"
    assert ident["source"] == "observe_target_at_handle"


def test_parse_returns_none_when_no_handle() -> None:
    obs = {
        "url": "https://x.com/home",
        "title": "Home / X",
        "targets": [
            {"role": "button", "name": "Post"},
            {"role": "link", "name": "Explore"},
        ],
    }
    # No @-handle and no handle-like single-segment name -> None (extract fallback runs).
    assert _parse_identity_from_observation(obs) is None


def test_parse_absolutizes_using_current_origin() -> None:
    obs = {
        "url": "https://twitter.com/home",  # twitter.com origin
        "title": "Home",
        "targets": [{"role": "link", "name": "@jack"}],
    }
    ident = _parse_identity_from_observation(obs)
    assert ident["profile_url"] == "https://twitter.com/jack"


def test_parse_handlelike_falls_back_when_no_at_prefix() -> None:
    """If no @-handle, a handle-like single-segment name is accepted conservatively."""
    obs = {
        "url": "https://x.com/home",
        "title": "Home",
        "targets": [{"role": "link", "name": "jackwener"}],
    }
    ident = _parse_identity_from_observation(obs)
    assert ident is not None
    assert ident["handle"] == "jackwener"
    assert ident["source"] == "observe_target_handlelike"


def test_parse_rejects_non_profile_roots() -> None:
    """Known non-profile path roots must not be mistaken for handles."""
    obs = {
        "url": "https://x.com/home",
        "title": "Home",
        "targets": [
            {"role": "link", "name": "Home"},
            {"role": "link", "name": "Explore"},
            {"role": "link", "name": "Notifications"},
        ],
    }
    assert _parse_identity_from_observation(obs) is None
