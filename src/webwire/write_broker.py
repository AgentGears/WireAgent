"""Write broker — scoped mutation surface, only accessible inside the WriteKernel
execute stage.

The ReadOnlyBroker (Phase 0a) has no mutating primitives by design. The WriteKernel
(Phase 0b) enforces compose→preview→policy→confirm before any mutation. The
execute stage receives THIS broker — a narrow, write-scoped surface that exposes
only the specific mutations each write action needs.

This broker is NEVER handed to read capabilities. It's constructed by the
dispatcher per-write (inside the kernel's execute path), used once, and discarded.
The kernel's confirmation gate is the only way to reach it.

Phase 3: bookmark only. Each new write action adds its scoped method here.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.kill_switch import KillSwitch

if TYPE_CHECKING:
    from super_browser import SuperBrowser

from super_browser.results.types import FailureCategory

logger = logging.getLogger(__name__)

__all__ = ["WriteBroker"]


class WriteBroker:
    """Narrow write surface. Only one mutation method per action type.

    Each method is scoped to exactly one DOM mutation — no generic click/fill.
    The method name encodes the action (click_bookmark, not click), so the
    write surface can't be repurposed for arbitrary mutations.
    """

    def __init__(self, sb: "SuperBrowser", kill_switch: KillSwitch) -> None:
        self._sb = sb
        self._kill = kill_switch

    def _guard(self):
        """Kill-switch check before any mutation."""
        return self._kill.guard()

    async def click_bookmark(self, post_url: str) -> ActionResult:
        """Click the bookmark button on the post at post_url.

        Navigates to the post, finds the bookmark button (data-testid='bookmark'),
        clicks it. Returns ok=True if the click succeeded.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        # Navigate to the post.
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)  # hydrate
        # Find and click the bookmark button.
        try:
            # X's bookmark button: data-testid='bookmark' (when not bookmarked).
            # After clicking, it becomes data-testid='removeBookmark'.
            click_result = await self._sb.click(
                "[data-testid='bookmark']",
                description="bookmark button",
            )
            if not click_result.ok:
                # Maybe already bookmarked (button is 'removeBookmark').
                already = await self._sb.click(
                    "[data-testid='removeBookmark']",
                    description="remove-bookmark button (already bookmarked)",
                )
                if already.ok:
                    return ok_result(data={"bookmarked": True, "note": "already_bookmarked"})
                return soft_failure(
                    f"Could not find bookmark button at {post_url!r}. "
                    f"DOM churn or post unavailable.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"bookmarked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(
                f"click_bookmark error: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        """Read-only check: is the post currently bookmarked? Used by verify()."""
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)
        # Check which bookmark button variant is present.
        from webwire.broker import ReadOnlyBroker
        # Reuse the CDP evaluate path to check button state.
        cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            "(function(){"
            "var bm=document.querySelector(\"[data-testid='bookmark']\");"
            "var rbm=document.querySelector(\"[data-testid='removeBookmark']\");"
            "if(rbm)return 'bookmarked';"
            "if(bm)return 'not_bookmarked';"
            "return 'unknown';"
            "})()"
        )
        try:
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                state = result.data.get("result", {}).get("value")
                return ok_result(data={"bookmark_state": state})
            return ok_result(data={"bookmark_state": "unknown"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"read_bookmark_state error: {exc!r}")

    # ------------------------------------------------------------------
    # Like port (LikeWritePort)
    # ------------------------------------------------------------------

    async def click_like(self, post_url: str) -> ActionResult:
        """Click the like button (directional — only when not liked).

        X's like button: data-testid='like' (when not liked). If already liked,
        the button is data-testid='unlike', and this method returns already_liked
        WITHOUT toggling. Compensation must use click_unlike, not this method.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)
        try:
            click_result = await self._sb.click(
                "[data-testid='like']",
                description="like button",
            )
            if not click_result.ok:
                # Maybe already liked — check, but DON'T toggle.
                state = await self.read_like_state(post_url)
                if state.ok and state.data and state.data.get("like_state") == "liked":
                    return ok_result(data={"liked": True, "note": "already_liked"})
                return soft_failure(
                    f"Could not find like button at {post_url!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"liked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_like error: {exc!r}")

    async def click_unlike(self, post_url: str) -> ActionResult:
        """Click the unlike button (directional — only when liked).

        Compensation primitive: clicks data-testid='unlike'. If not liked,
        returns already_not_liked WITHOUT toggling. This is the directional
        inverse of click_like, per ChatGPT's rule: 'Write methods must be
        semantic, not toggle-based.'
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)
        try:
            click_result = await self._sb.click(
                "[data-testid='unlike']",
                description="unlike button (compensation)",
            )
            if not click_result.ok:
                # Maybe already not liked — check, DON'T toggle.
                state = await self.read_like_state(post_url)
                if state.ok and state.data and state.data.get("like_state") == "not_liked":
                    return ok_result(data={"liked": False, "note": "already_not_liked"})
                return soft_failure(
                    f"Could not find unlike button at {post_url!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"liked": False, "unliked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_unlike error: {exc!r}")

    async def read_like_state(self, post_url: str) -> ActionResult:
        """Read-only check: is the post currently liked?"""
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)
        cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            "(function(){"
            "var l=document.querySelector(\"[data-testid='like']\");"
            "var ul=document.querySelector(\"[data-testid='unlike']\");"
            "if(ul)return 'liked';"
            "if(l)return 'not_liked';"
            "return 'unknown';"
            "})()"
        )
        try:
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                state = result.data.get("result", {}).get("value")
                return ok_result(data={"like_state": state})
            return ok_result(data={"like_state": "unknown"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"read_like_state error: {exc!r}")
