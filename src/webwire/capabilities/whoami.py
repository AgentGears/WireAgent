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
import time
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

        # 2. Observe — deterministic AX snapshot.
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

        # 4. Deterministic parse off the AX snapshot targets.
        identity = _parse_identity_from_observation(obs_data)
        if identity is not None and identity.get("handle") and identity.get("profile_url"):
            identity["session_status"] = "authenticated"
            return ok_result(data=identity, success_category=SuccessCategory.INSPECTION)

        # 5. Fallback: extract with a schema. Only if observe could not resolve it.
        logger.info("whoami: deterministic parse incomplete; falling back to extract()")
        ext = await broker.extract(
            "my account handle, display name, profile URL",
            schema={
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "display_name": {"type": "string"},
                    "profile_url": {"type": "string"},
                },
                "required": ["handle", "profile_url"],
            },
        )
        if not ext.ok:
            # Authenticated but identity unresolvable via both observe-parse and
            # extract. Review-iteration adjustment: encode the operational
            # condition as a distinct code so the journal distinguishes this
            # from generic selector drift. This is a soft FAILURE at runtime
            # (likely DOM churn, recoverable) but a HARD BLOCKER at the Phase
            # 0a acceptance gate (identity is foundational).
            return soft_failure(
                "identity_unresolved_on_authenticated_surface: authenticated session "
                "but could not resolve identity (observe-parse and extract both failed). "
                "Likely X DOM churn — update _parse_identity_from_observation.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                retry_hint="Update the identity parser in whoami.py and retry. "
                "Blocks Phase 1 until whoami resolves on a live authenticated session.",
            )

        ext_data: dict[str, Any] = ext.data or {}
        identity = {
            "handle": ext_data.get("handle"),
            "display_name": ext_data.get("display_name"),
            "profile_url": ext_data.get("profile_url"),
            "session_status": "authenticated",
            "source": "extract_fallback",
        }
        return ok_result(data=identity)


# ---------------------------------------------------------------------------
# Helpers — the maintenance-sensitive surface
# ---------------------------------------------------------------------------

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
    non_profile_roots = {
        "home", "explore", "notifications", "messages", "search", "compose",
        "settings", "i", "login", "signup", "tos", "privacy",
        "post", "tweet", "reply", "share", "more", "back", "next", "close",
        "follow", "following", "followers", "likes", "bookmarks", "profile",
    }
    for t in targets:
        if t.get("role") != "link":
            continue
        name = (t.get("name") or "").strip()
        if not name or name.startswith("@"):
            continue
        # Only accept if it really looks like a handle (alnum + underscore).
        tentative = name.lstrip("@")
        if (
            tentative
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
