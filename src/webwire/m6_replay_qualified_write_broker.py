"""M6 Layer-7 live broker qualification for like/unlike replay behavior.

This class deliberately does not promote replay policy. It strengthens the
supported live broker path so the directional DOM state used by like/unlike is
fail-closed under contradictory/hydrating selectors and is revalidated at the
post-authority click seam.

The stronger external claim required for ``ReplaySemantics.SAFE_STATE_SET`` is
separate: DOM convergence does not prove absence of notifications, callbacks,
analytics, counter transitions, or other residual public-engagement effects.
"""

from __future__ import annotations

import asyncio

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker

__all__ = ["M6ReplayQualifiedWriteBroker"]


class M6ReplayQualifiedWriteBroker(M5LeasedWriteBroker):
    """Supported live M5 broker with the M6 Layer-7 engagement hardening."""

    _LIKE_SETTLE_SECONDS = 4.0
    _LIKE_HYDRATION_ATTEMPTS = 4
    _LIKE_HYDRATION_INTERVAL_SECONDS = 0.25

    @staticmethod
    def _authoritative_control_predicate_js() -> str:
        """Return a conservative DOM predicate for mutation/state authority."""

        return (
            "function authoritative(el){"
            "if(!(el&&el.isConnected&&el.getClientRects().length))return false;"
            "var s=getComputedStyle(el);"
            "if(s.display==='none'||s.visibility==='hidden'||s.visibility==='collapse'"
            "||s.pointerEvents==='none')return false;"
            "if(el.getAttribute('aria-hidden')==='true'||el.getAttribute('aria-disabled')==='true')"
            "return false;"
            "if(el.matches&&el.matches(':disabled'))return false;"
            "return true;}"
        )

    @staticmethod
    def _direct_authoritative_controls_js(testid: str, variable: str) -> str:
        """Collect authoritative controls owned directly by the target article.

        A quoted/nested article is not the approved target. Multiple usable
        controls in the same direction are ambiguous rather than allowing DOM
        ordering to choose authority accidentally.
        """

        return (
            f"var {variable}=[];"
            f"var {variable}Nodes=art.querySelectorAll(\"[data-testid='{testid}']\");"
            f"for(var {variable}i=0;{variable}i<{variable}Nodes.length;{variable}i++){{"
            f"var {variable}El={variable}Nodes[{variable}i];"
            f"if({variable}El.closest('article')===art&&authoritative({variable}El))"
            f"{variable}.push({variable}El);}}"
        )

    @staticmethod
    def _like_state_expr(post_id: str) -> str:
        return (
            M6ReplayQualifiedWriteBroker._target_article_prefix(
                post_id,
                "m6-layer7-like-state",
            )
            + M6ReplayQualifiedWriteBroker._authoritative_control_predicate_js()
            + M6ReplayQualifiedWriteBroker._direct_authoritative_controls_js("like", "likes")
            + M6ReplayQualifiedWriteBroker._direct_authoritative_controls_js("unlike", "unlikes")
            + "if(likes.length===1&&unlikes.length===0)return 'not_liked';"
            + "if(unlikes.length===1&&likes.length===0)return 'liked';"
            + "return 'unknown';})()"
        )

    async def read_like_state(self, post_url: str) -> ActionResult:
        """Read one target's directional like state conservatively.

        A short bounded hydration window tolerates selector appearance after
        navigation. Contradictory, hidden, disabled, disconnected, nested, or
        duplicate controls are never positive state evidence; if exactly one
        direct authoritative direction does not become observable, the result
        remains unknown.
        """

        if (guarded := self._guard()) is not None:
            return guarded
        post_id = self._status_id(post_url)
        if post_id is None:
            return soft_failure(
                f"invalid scoped status URL: {post_url!r}",
                failure_category=FailureCategory.SECURITY,
            )
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        if self._LIKE_SETTLE_SECONDS > 0:
            await asyncio.sleep(self._LIKE_SETTLE_SECONDS)

        expr = self._like_state_expr(post_id)
        try:
            for attempt in range(self._LIKE_HYDRATION_ATTEMPTS):
                result = await self._sb._controller._cdp.evaluate(expr)
                if result.ok and result.data and "exceptionDetails" not in result.data:
                    state = result.data.get("result", {}).get("value") or "unknown"
                    if state in {"liked", "not_liked"}:
                        return ok_result(data={"like_state": state})
                if attempt + 1 < self._LIKE_HYDRATION_ATTEMPTS:
                    await asyncio.sleep(self._LIKE_HYDRATION_INTERVAL_SECONDS)
            return ok_result(data={"like_state": "unknown"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"target like-state read error: {exc!r}")

    @staticmethod
    def _qualified_like_click_expr(post_id: str, testid: str) -> str:
        expected = "like" if testid == "like" else "unlike"
        opposite = "unlike" if expected == "like" else "like"
        return (
            M6ReplayQualifiedWriteBroker._target_article_prefix(
                post_id,
                f"m6-layer7-like-click-{expected}",
            )
            + M6ReplayQualifiedWriteBroker._authoritative_control_predicate_js()
            + M6ReplayQualifiedWriteBroker._direct_authoritative_controls_js(expected, "expected")
            + M6ReplayQualifiedWriteBroker._direct_authoritative_controls_js(opposite, "opposite")
            + "if(expected.length!==1||opposite.length!==0){"
            + "if(expected.length||opposite.length)return 'ambiguous';"
            + "return 'stale';}"
            + "expected[0].click();return 'clicked';})()"
        )

    async def _click_target_control(
        self,
        post_url: str,
        *,
        testid: str,
        description: str,
    ) -> ActionResult:
        """Revalidate like/unlike direction at the exact post-authority seam."""

        if testid not in {"like", "unlike"}:
            return await super()._click_target_control(
                post_url,
                testid=testid,
                description=description,
            )

        post_id = self._status_id(post_url)
        if post_id is None:
            return soft_failure(
                f"invalid scoped status URL: {post_url!r}",
                failure_category=FailureCategory.SECURITY,
            )
        try:
            result = await self._sb._controller._cdp.evaluate(
                self._qualified_like_click_expr(post_id, testid)
            )
            value = (
                result.data.get("result", {}).get("value")
                if result.ok and result.data
                else None
            )
            if value != "clicked":
                return soft_failure(
                    f"Could not safely resolve {description} on approved target "
                    f"{post_id!r}: {value!r}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"clicked": True, "post_id": post_id})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(
                f"target {description} click error: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )
