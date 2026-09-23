"""M5-specific WriteBroker subclass with exact scoped commit seams.

Layer 4 leaves the legacy WriteBroker/live WriteKernel untouched. Layer 5 will
place this broker behind scoped authority objects. Canonical mutation methods
fail closed unless a private commit gate is supplied, and submit additionally
requires a scoped precommit payload check.

State-set engagement operations are target-scoped: both the state probe and the
actual directional click resolve the article whose timestamp link owns the
approved ``/status/<id>``. Navigating to a status page alone is not considered a
target binding because a page can contain multiple articles.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from typing import Optional
from urllib.parse import urlparse

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.write_broker import WriteBroker

__all__ = ["M5WriteBroker"]

CommitGate = Callable[[], Optional[ActionResult]]
PrecommitCheck = Callable[[], Awaitable[Optional[ActionResult]]]

_STATUS_HOSTS = frozenset({"x.com", "www.x.com", "twitter.com", "www.twitter.com"})
_STATUS_PATH_RE = re.compile(r"/status/(\d+)(?:/|$)")


class M5WriteBroker(WriteBroker):
    """Concrete M5 mutation seam; every canonical effect requires authority."""

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

    @staticmethod
    def _status_id(post_url: str) -> Optional[str]:
        parsed = urlparse(post_url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme.lower() != "https" or host not in _STATUS_HOSTS:
            return None
        match = _STATUS_PATH_RE.search(parsed.path)
        return match.group(1) if match is not None else None

    @staticmethod
    def _target_article_prefix(post_id: str, marker: str) -> str:
        """JS that resolves the article whose own timestamp link has post_id."""
        # ``post_id`` is numeric by construction; repr keeps the JS literal safe.
        target = repr(post_id)
        return (
            "(function(){"
            f"/*{marker}:{post_id}*/"
            f"var targetId={target};"
            "var arts=document.querySelectorAll('article');"
            "var art=null;"
            "for(var ai=0;ai<arts.length&&!art;ai++){"
            "var links=arts[ai].querySelectorAll('a[href]');"
            "for(var li=0;li<links.length;li++){"
            "var a=links[li];"
            "if(!a.querySelector('time'))continue;"
            "try{var u=new URL(a.href,location.href);"
            "if(u.pathname.endsWith('/status/'+targetId)){art=arts[ai];break;}}"
            "catch(e){}"
            "}"
            "}"
            "if(!art)return 'target_missing';"
        )

    async def _target_state(
        self,
        post_url: str,
        *,
        set_testid: str,
        clear_testid: str,
        set_state: str,
        clear_state: str,
        data_key: str,
        settle_seconds: float,
    ) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        post_id = self._status_id(post_url)
        if post_id is None:
            return soft_failure(
                f"invalid scoped status URL: {post_url!r}",
                failure_category=FailureCategory.SECURITY,
            )
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)

        expr = (
            self._target_article_prefix(post_id, f"m5-target-state-{data_key}")
            + f"var clear=art.querySelector(\"[data-testid='{clear_testid}']\");"
            + f"var set=art.querySelector(\"[data-testid='{set_testid}']\");"
            + f"if(clear)return {clear_state!r};"
            + f"if(set)return {set_state!r};"
            + "return 'unknown';})()"
        )
        try:
            result = await self._sb._controller._cdp.evaluate(expr)
            if result.ok and result.data and "exceptionDetails" not in result.data:
                state = result.data.get("result", {}).get("value") or "unknown"
                return ok_result(data={data_key: state})
            return ok_result(data={data_key: "unknown"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"target state read error: {exc!r}")

    async def _click_target_control(
        self,
        post_url: str,
        *,
        testid: str,
        description: str,
    ) -> ActionResult:
        post_id = self._status_id(post_url)
        if post_id is None:
            return soft_failure(
                f"invalid scoped status URL: {post_url!r}",
                failure_category=FailureCategory.SECURITY,
            )
        expr = (
            self._target_article_prefix(post_id, f"m5-target-click-{testid}")
            + f"var b=art.querySelector(\"[data-testid='{testid}']\");"
            + "if(!b)return 'control_missing';"
            + "b.click();return 'clicked';})()"
        )
        try:
            result = await self._sb._controller._cdp.evaluate(expr)
            value = (
                result.data.get("result", {}).get("value")
                if result.ok and result.data
                else None
            )
            if value != "clicked":
                return soft_failure(
                    f"Could not find {description} on approved target {post_id!r}.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            return ok_result(data={"clicked": True, "post_id": post_id})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(
                f"target {description} click error: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        return await self._target_state(
            post_url,
            set_testid="bookmark",
            clear_testid="removeBookmark",
            set_state="not_bookmarked",
            clear_state="bookmarked",
            data_key="bookmark_state",
            settle_seconds=self._BOOKMARK_SETTLE_S,
        )

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """State-first SET_BOOKMARK scoped to the approved status article."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_bookmark_state(post_url)
        state = (
            (state_r.data or {}).get("bookmark_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "bookmarked":
            return ok_result(data={"bookmarked": True, "result": "already_satisfied"})
        if state != "not_bookmarked":
            return soft_failure(
                f"click_bookmark: unresolved target bookmark state {state!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        clicked = await self._click_target_control(
            post_url,
            testid="bookmark",
            description="bookmark button",
        )
        if not clicked.ok:
            return clicked
        return ok_result(data={"bookmarked": True})

    async def click_remove_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """State-first CLEAR_BOOKMARK scoped to the approved status article."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_bookmark_state(post_url)
        state = (
            (state_r.data or {}).get("bookmark_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "not_bookmarked":
            return ok_result(data={"bookmarked": False, "result": "already_satisfied"})
        if state != "bookmarked":
            return soft_failure(
                f"click_remove_bookmark: unresolved target bookmark state {state!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        clicked = await self._click_target_control(
            post_url,
            testid="removeBookmark",
            description="remove-bookmark button",
        )
        if not clicked.ok:
            return clicked
        return ok_result(data={"bookmarked": False})

    async def read_like_state(self, post_url: str) -> ActionResult:
        return await self._target_state(
            post_url,
            set_testid="like",
            clear_testid="unlike",
            set_state="not_liked",
            clear_state="liked",
            data_key="like_state",
            settle_seconds=4.0,
        )

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Directional SET_LIKE scoped to the approved status article."""
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
                f"click_like: unresolved target like state {state!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        clicked = await self._click_target_control(
            post_url,
            testid="like",
            description="like button",
        )
        if not clicked.ok:
            return clicked
        return ok_result(data={"liked": True})

    async def click_unlike(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Directional CLEAR_LIKE scoped to the approved status article."""
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_like_state(post_url)
        state = (
            (state_r.data or {}).get("like_state", "unknown")
            if state_r.ok
            else "unknown"
        )
        if state == "not_liked":
            return ok_result(data={"liked": False, "note": "already_not_liked"})
        if state != "liked":
            return soft_failure(
                f"click_unlike: unresolved target like state {state!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        clicked = await self._click_target_control(
            post_url,
            testid="unlike",
            description="unlike button",
        )
        if not clicked.ok:
            return clicked
        return ok_result(data={"liked": False, "unliked": True})

    async def click_submit(
        self,
        *,
        _commit_gate: Optional[CommitGate] = None,
        _precommit_check: Optional[PrecommitCheck] = None,
    ) -> ActionResult:
        """Submit only after scoped payload proof, then consume at final click."""
        if (r := self._guard()) is not None:
            return r
        if _precommit_check is None:
            return soft_failure(
                "M5 submit requires scoped payload verification",
                failure_category=FailureCategory.SECURITY,
            )
        checked = await _precommit_check()
        if checked is not None:
            return checked
        # Verification may await browser I/O. Re-check local kill state before
        # the gateway performs its own final authority-boundary checks.
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
        """Abort cleanup is reducing authority and remains available under kill."""
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
                "https://x.com/home",
                wait_until="domcontentloaded",
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
        """Id-scoped delete with permit consumption at final confirmation."""
        if (r := self._guard()) is not None:
            return r
        if self._status_id(post_url) != post_id:
            return soft_failure(
                "delete_post target URL/id mismatch",
                failure_category=FailureCategory.SECURITY,
            )
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
                    "delete_post: target post not found (deleted already, not yours, or URL invalid)",
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
                    'if(t==="Delete"||t==="Delete post"||t==="删除"||t==="删除帖子"){' 
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
