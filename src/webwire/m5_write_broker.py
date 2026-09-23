"""M5-specific WriteBroker subclass with exact commit-gate seams.

Layer 4 deliberately leaves the legacy WriteBroker/live WriteKernel untouched.
This subclass is the concrete broker that Layer 5 will place behind scoped
authority objects. Effect-producing methods accept a private ``_commit_gate``
keyword and invoke it immediately before the canonical mutating click.

The keyword is optional only so this class remains substitutable for the legacy
WriteBroker at the Python type level. Omitting it always fails closed before the
canonical effect; Layer 5 capabilities receive scoped authorities, not this
object directly.

Preparation/read methods are inherited unchanged. Capabilities must never receive
this object directly once Layer 5 is wired; they receive scoped authorities.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.write_broker import WriteBroker

__all__ = ["M5WriteBroker"]

CommitGate = Callable[[], Optional[ActionResult]]


class M5WriteBroker(WriteBroker):
    """Concrete M5 mutation seam; every canonical effect requires a commit hook."""

    @staticmethod
    def _cross_commit_gate(
        commit_gate: Optional[CommitGate],
    ) -> Optional[ActionResult]:
        if commit_gate is None:
            return soft_failure(
                "M5 canonical effect requires scoped commit authority",
                failure_category=FailureCategory.SECURITY,
            )
        denied = commit_gate()
        if denied is not None:
            return denied
        return None

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """State-first SET_BOOKMARK; consume authority only before the click."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_bookmark_state(post_url)
        state = (
            (state_r.data or {}).get("bookmark_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "bookmarked":
            return ok_result(
                data={"bookmarked": True, "result": "already_satisfied"}
            )
        if state != "not_bookmarked":
            return soft_failure(
                f"click_bookmark: unresolved bookmark state {state!r} at "
                f"{post_url!r} — refusing to mutate on an unknown state.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            click_result = await self._sb.click(
                "[data-testid='bookmark']",
                description="bookmark button",
            )
            if not click_result.ok:
                return soft_failure(
                    f"Could not find bookmark button at {post_url!r}. "
                    "DOM churn or post unavailable.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"bookmarked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(
                f"click_bookmark error: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

    async def click_remove_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """State-first CLEAR_BOOKMARK; consume authority only before the click."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_bookmark_state(post_url)
        state = (
            (state_r.data or {}).get("bookmark_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "not_bookmarked":
            return ok_result(
                data={"bookmarked": False, "result": "already_satisfied"}
            )
        if state != "bookmarked":
            return soft_failure(
                f"click_remove_bookmark: unresolved bookmark state {state!r} at "
                f"{post_url!r} — refusing to mutate on an unknown state.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            click_result = await self._sb.click(
                "[data-testid='removeBookmark']",
                description="remove-bookmark button",
            )
            if not click_result.ok:
                return soft_failure(
                    f"Could not find remove-bookmark button at {post_url!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"bookmarked": False})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(
                f"click_remove_bookmark error: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Directional SET_LIKE with an exact pre-click commit boundary.

        Layer 4 makes the M5 seam state-first so a known already-liked state does
        not consume a permit. Replay policy nevertheless remains UNKNOWN until
        this behavior has its own concrete regression evidence and review.
        """
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_like_state(post_url)
        state = (
            (state_r.data or {}).get("like_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "liked":
            return ok_result(data={"liked": True, "note": "already_liked"})
        if state != "not_liked":
            return soft_failure(
                f"click_like: unresolved like state {state!r} at {post_url!r}; "
                "refusing to mutate on an unknown state.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            click_result = await self._sb.click(
                "[data-testid='like']",
                description="like button",
            )
            if not click_result.ok:
                return soft_failure(
                    f"Could not find like button at {post_url!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"liked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_like error: {exc!r}")

    async def click_unlike(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Directional CLEAR_LIKE with an exact pre-click commit boundary."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_like_state(post_url)
        state = (
            (state_r.data or {}).get("like_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "not_liked":
            return ok_result(
                data={"liked": False, "note": "already_not_liked"}
            )
        if state != "liked":
            return soft_failure(
                f"click_unlike: unresolved like state {state!r} at {post_url!r}; "
                "refusing to mutate on an unknown state.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            click_result = await self._sb.click(
                "[data-testid='unlike']",
                description="unlike button (compensation)",
            )
            if not click_result.ok:
                return soft_failure(
                    f"Could not find unlike button at {post_url!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"liked": False, "unliked": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_unlike error: {exc!r}")

    async def click_submit(
        self,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Consume SUBMIT_CONTENT immediately before the tweet-button click."""
        if (r := self._guard()) is not None:
            return r
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            return await self._sb.click(
                "[data-testid='tweetButton']",
                description="post submit button",
            )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_submit error: {exc!r}")

    async def close_composer(self) -> ActionResult:
        """Abort cleanup is reducing authority and stays available under kill.

        The legacy WriteBroker retains its historical guarded behavior until
        Layer 5. The M5 broker deliberately permits only this cleanup override
        after kill/revocation; it cannot publish content.
        """
        import asyncio

        try:
            cdp = self._sb._controller._cdp
            close_expr = (
                '(function(){'
                'var btns=document.querySelectorAll("button");'
                'for(var i=0;i<btns.length;i++){'
                'var aria=btns[i].getAttribute("aria-label")||"";'
                'if(aria.indexOf("Close")>=0||aria.indexOf("Cancel")>=0){'
                'btns[i].click();return "closed";}'
                '}'
                'return "no_close_button";'
                '})()'
            )
            result = await cdp.evaluate(close_expr)
            state = (
                result.data.get("result", {}).get("value")
                if result.ok and result.data
                else None
            )
            await asyncio.sleep(1)
            await self._sb.navigate(
                "https://x.com/home", wait_until="domcontentloaded"
            )
            await asyncio.sleep(2)
            return ok_result(data={"cleanup": state or "navigated_away"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"close_composer error: {exc!r}")

    async def delete_post(
        self,
        post_url: str,
        post_id: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Id-scoped delete with permit consumption at final confirmation.

        Navigation, target/caret/menu staging and confirmation-control polling do
        not consume the permit. The commit hook runs only after a confirmation
        control is known to exist and the final broker kill check passes.
        """
        if (r := self._guard()) is not None:
            return r
        try:
            nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
            if not nav.ok:
                return nav

            r1 = await self._delete_poll(
                lambda: self._delete_eval(
                    self._delete_article_js(post_id, 'return "found";')
                ),
                want_true=True,
                label="target article",
            )
            if not r1.ok:
                return soft_failure(
                    "delete_post: target post not found (deleted already, "
                    "not yours, or URL invalid)",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            r2 = await self._delete_eval(
                self._delete_article_js(
                    post_id,
                    'var c=art.querySelector("[data-testid=\'caret\']");'
                    'if(!c)return null;c.click();return "caret_clicked";',
                )
            )
            if not (r2.ok and r2.data):
                return soft_failure(
                    "delete_post: caret button not found on target article",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            r3 = await self._delete_poll(
                lambda: self._delete_eval(
                    '(function(){'
                    'var menus=document.querySelectorAll('
                    '"[data-testid=\'Dropdown\'],[role=\'menu\']");'
                    'for(var m=0;m<menus.length;m++){'
                    'var items=menus[m].querySelectorAll('
                    '"[role=\'menuitem\'],a,button");'
                    'for(var i=0;i<items.length;i++){'
                    'var t=(items[i].innerText||"").trim();'
                    'if(t==="Delete"||t==="Delete post"||t==="删除"'
                    '||t==="删除帖子"){'
                    'items[i].click();return "delete_item_clicked:"+t;}}}'
                    'return null;})()'
                ),
                want_true=True,
                label="delete menu item",
            )
            if not r3.ok:
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: Delete item not in menu (post may not be yours)",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            ready_expr = (
                '(function(){'
                'var b=document.querySelector('
                '"[data-testid=\'confirmationSheetConfirm\']");'
                'if(!b){var btns=document.querySelectorAll("button");'
                'for(var i=0;i<btns.length;i++){'
                'var t=(btns[i].innerText||"").trim();'
                'if(t==="Delete"){b=btns[i];break;}}}'
                'return b?"ready":null;})()'
            )
            r4_ready = await self._delete_poll(
                lambda: self._delete_eval(ready_expr),
                want_true=True,
                label="confirmation button",
            )
            if not r4_ready.ok:
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: confirmation button never appeared",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            if (r := self._guard()) is not None:
                await self._dismiss_delete_dialog()
                return r
            if (denied := self._cross_commit_gate(_commit_gate)) is not None:
                await self._dismiss_delete_dialog()
                return denied

            r4 = await self._delete_eval(
                '(function(){'
                'var b=document.querySelector('
                '"[data-testid=\'confirmationSheetConfirm\']");'
                'if(!b){var btns=document.querySelectorAll("button");'
                'for(var i=0;i<btns.length;i++){'
                'var t=(btns[i].innerText||"").trim();'
                'if(t==="Delete"){b=btns[i];break;}}}'
                'if(!b)return null;b.click();return "confirm_clicked";})()'
            )
            if not (r4.ok and r4.data):
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: confirmation control disappeared before click",
                    failure_category=FailureCategory.UNKNOWN,
                )

            r5 = await self._delete_poll(
                lambda: self._delete_eval(
                    '(function(){return document.querySelector('
                    '"[data-testid=\'confirmationSheetConfirm\']")===null;})()'
                ),
                want_true=True,
                label="confirmation sheet dismissed",
            )
            return ok_result(
                data={
                    "deleted": True,
                    "post_id": post_id,
                    "sheet_dismissed": bool(r5.ok),
                }
            )
        except Exception as exc:  # noqa: BLE001
            await self._dismiss_delete_dialog()
            return soft_failure(f"delete_post error: {exc!r}")
