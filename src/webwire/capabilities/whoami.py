"""whoami capability — resolve the logged-in account identity.

Per the Phase 0a design (Point 5 decision):
- Primary path: observe() -> deterministic parse for account/profile affordances.
- Fallback/enrichment: extract(schema=...) only if observation cannot confidently
  produce the full identity.
- On login wall: return failure_category=AUTH_REQUIRED.

Identity is foundational, so it is as deterministic and auditable as possible.
The parser is deliberately isolated in :func:`_parse_identity_from_observation`
so it is the single thing to fix when X's DOM churns.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.broker import ReadOnlyBroker
from webwire.capabilities.base import CapabilityTier
from webwire.envelope import (
    ActionResult,
    auth_required,
    ok_result,
    soft_failure,
)

logger = logging.getLogger(__name__)

__all__ = ["WhoamiCapability", "Identity"]


# Minimal identity payload. Keeping it small reduces coupling to X's DOM.
class Identity(dict):
    """str-keyed dict subclass for typed-ish identity data."""


# Login-wall heuristics. If the observed URL/title matches these, we are not
# authenticated. Kept conservative to avoid false positives on legit pages.
_LOGIN_WALL_URL_FRAGMENTS = ("/login", "/i/flow/login", "oauth/authorize")
_LOGIN_WALL_TITLE_FRAGMENTS = ("log in", "sign in", "sign up for x")


class WhoamiCapability:
    """Resolve handle, display name, profile URL, and session status."""

    name = "whoami"
    tier = CapabilityTier.READ

    async def run(self, broker: ReadOnlyBroker, input: dict[str, Any]) -> ActionResult:
        home_url = input.get("home_url") or "https://x.com/home"

        # 1. Navigate to home.
        nav = await broker.navigate(home_url)
        if not nav.ok:
            return nav  # propagate navigation failure (TIMEOUT/NAVIGATION/SECURITY)

        # 1b. X is a React SPA — content isn't ready at domcontentloaded. Wait
        # for hydration before observing, or the snapshot is empty/partial.
        import asyncio as _asyncio
        await _asyncio.sleep(4)

        # 2. Observe — AX snapshot for login-wall detection + fast parse.
        obs = await broker.observe()
        if not obs.ok:
            return obs

        obs_data: dict[str, Any] = obs.data or {}
        url: str = obs_data.get("url", "") or ""
        title: str = obs_data.get("title", "") or ""

        # 3. Login-wall detection.
        if _looks_like_login_wall(url, title):
            return auth_required(
                f"Login wall detected at {url!r}. Re-acquire an authenticated session."
            )

        # 4. PRIMARY path: read the Profile nav link's href via query_attr.
        # X's home left-rail has a stable Profile link (data-testid=
        # AppTabBar_Profile_Link) whose href is /<handle>. This is the most
        # deterministic identity source — it comes straight from X's own nav,
        # not from heuristics on element names. query_attr reads the href
        # attribute via read-only CDP getAttribute.
        logger.info("whoami: reading Profile link href from DOM (primary path)")
        profile_href = await _read_profile_href(broker)
        identity: dict[str, Any] | None = None
        if profile_href:
            handle = profile_href.strip("/").split("/")[-1]
            if handle and "/" not in handle and len(handle) >= 2:
                identity = {
                    "handle": handle,
                    "display_name": None,
                    "profile_url": _absolutize_path(profile_href, url),
                    "session_status": "authenticated",
                    "source": "profile_link_href",
                }
                return ok_result(data=identity, success_category=SuccessCategory.INSPECTION)

        # 5. Fallback: deterministic parse off the AX snapshot targets.
        # (Weaker — relies on @-handle link names, which X often doesn't expose
        #  on the home timeline. Kept as fallback for surfaces that do.)
        parsed = _parse_identity_from_observation(obs_data)
        if parsed is not None and parsed.get("handle") and parsed.get("profile_url"):
            parsed["session_status"] = "authenticated"
            identity = parsed
            return ok_result(data=identity, success_category=SuccessCategory.INSPECTION)

        # 6. Last resort: scan the AX compact string for a handle-like path.
        ext = await broker.extract("identity")
        if ext.ok and ext.data:
            extracted_str: str = (
                ext.data.get("extracted") if isinstance(ext.data, dict) else str(ext.data)
            ) or ""
            scanned = _scan_for_handle_in_ax(extracted_str, obs_data)
            if scanned:
                identity = {
                    "handle": handle,
                    "display_name": None,
                    "profile_url": _absolutize_path(f"/{handle}", url),
                    "session_status": "authenticated",
                    "source": "ax_scan",
                }
                return ok_result(data=identity, success_category=SuccessCategory.INSPECTION)

        return soft_failure(
            "identity_unresolved_on_authenticated_surface: authenticated session "
            "but could not resolve identity (AX-parse, profile-href, and AX-scan all failed). "
            "Likely X DOM churn — update whoami.",
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            retry_hint="Update the identity parser in whoami.py and retry. "
            "Blocks Phase 1 until whoami resolves on a live authenticated session.",
        )


# ---------------------------------------------------------------------------
# Helpers — the maintenance-sensitive surface
# ---------------------------------------------------------------------------

async def _read_profile_href(broker: ReadOnlyBroker) -> Optional[str]:
    """Read the logged-in user's profile path via the Profile nav link's href.

    X's left rail has a stable Profile link whose href is /<handle>. We read the
    href attribute via broker.query_attr (read-only CDP getAttribute), since
    observe() (AX snapshot) and extract(selector) (textContent) can't read attrs.
    Tries X's known testids/aria-labels, most-stable first.
    """
    # (selector, attr) pairs to try. X's Profile link: data-testid or aria-label.
    probes = [
        ("[data-testid='AppTabBar_Profile_Link']", "href"),
        ("a[aria-label='Profile']", "href"),
        ("[data-testid='SideNav_AccountSwitcher_Button']", "href"),
        # Account menu button sometimes carries the handle in aria-label
        ("[aria-label='Account menu']", "aria-label"),
    ]
    for selector, attr in probes:
        r = await broker.query_attr(selector, attr)
        if not r.ok or not r.data:
            continue
        value = r.data.get("value") if isinstance(r.data, dict) else None
        if not value:
            continue
        # href is /<handle>; aria-label may be "Account menu" or contain @handle
        if value.startswith("/") and len(value) > 1:
            return value
        if value.startswith("@"):
            return "/" + value.lstrip("@")
    return None


def _scan_for_handle_in_ax(ax_str: str, obs_data: dict[str, Any]) -> Optional[str]:
    """Last-resort: scan the AX compact string for a handle-like path near
    account/profile affordances. Best-effort; returns None if uncertain."""
    import re
    # Look for /<handle> patterns (single path segment, alnum/underscore).
    # Prefer ones appearing near "Account" or "Profile" context.
    candidates = re.findall(r"/([A-Za-z0-9_]{1,15})\b", ax_str)
    # Filter out obvious non-handles.
    non_profile = {
        "home", "explore", "notifications", "messages", "search", "compose",
        "settings", "i", "login", "signup", "tos", "privacy",
    }
    for c in candidates:
        if c.lower() not in non_profile and len(c) >= 2:
            return c
    return None


def _looks_like_login_wall(url: str, title: str) -> bool:
    url_l = url.lower()
    title_l = title.lower()
    if any(frag in url_l for frag in _LOGIN_WALL_URL_FRAGMENTS):
        return True
    if any(frag in title_l for frag in _LOGIN_WALL_TITLE_FRAGMENTS):
        return True
    return False


def _parse_identity_from_observation(obs_data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Deterministic identity parse from an observe() snapshot.

    Looks for the account affordance in the AX target list. X renders the
    logged-in user's profile link as an anchor to ``/<handle>`` with an
    ``aria-label`` / name like "@handle" or the user's display name, typically
    with ``role=link``. We match conservatively and prefer explicit @-handles.

    This is THE function to edit when X changes its DOM. It is intentionally
    defensive: returns None when uncertain so the extract() fallback runs.
    """
    targets: list[dict[str, Any]] = obs_data.get("targets", []) or []
    current_url: str = obs_data.get("url", "") or ""

    # Strategy A: find a link whose name starts with "@" -> handle.
    for t in targets:
        name = (t.get("name") or "").strip()
        if name.startswith("@") and len(name) > 1:
            handle = name[1:]
            profile_url = _absolutize_path(f"/{handle}", current_url)
            return {
                "handle": handle,
                "display_name": None,  # not always available from this target
                "profile_url": profile_url,
                "source": "observe_target_at_handle",
            }

    # Strategy B: find a LINK (profile affordances are links, not buttons) to a
    # /<handle> path that looks like a profile. Restricting to role=link avoids
    # matching UI labels like "Post" / "Explore" that are buttons or menu items.
    # Heuristic: single-segment, alnum+underscore, not a known non-profile root.
    # MUST reject single-char names and brand names — "X" (the logo link) is a
    # classic false positive that previously produced a wrong identity.
    non_profile_roots = {
        "home", "explore", "notifications", "messages", "search", "compose",
        "settings", "i", "login", "signup", "tos", "privacy",
        "post", "tweet", "reply", "share", "more", "back", "next", "close",
        "follow", "following", "followers", "likes", "bookmarks", "profile",
        "x", "grok", "chat", "subscribe", "premium",  # brand/nav false positives
    }
    for t in targets:
        if t.get("role") != "link":
            continue
        name = (t.get("name") or "").strip()
        if not name or name.startswith("@"):
            continue
        # Only accept if it really looks like a handle (alnum + underscore),
        # length >= 2 (single-char like "X" is never a real handle), and not a
        # known brand/nav label.
        tentative = name.lstrip("@")
        if (
            len(tentative) >= 2
            and tentative.lower() not in non_profile_roots
            and all(c.isalnum() or c == "_" for c in tentative)
            and 1 <= len(tentative) <= 15
        ):
            return {
                "handle": tentative,
                "display_name": None,
                "profile_url": _absolutize_path(f"/{tentative}", current_url),
                "source": "observe_target_handlelike",
            }

    return None


def _absolutize_path(path: str, current_url: str) -> str:
    """Turn a site-relative path into an absolute URL using current_url's origin."""
    if path.startswith("http"):
        return path
    if "://" in current_url:
        scheme, rest = current_url.split("://", 1)
        origin = rest.split("/", 1)[0]
        return f"{scheme}://{origin}{path}"
    return path
