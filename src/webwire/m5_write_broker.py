"""M5-specific WriteBroker subclass with exact scoped commit seams.

Layer 4 leaves the legacy WriteBroker/live WriteKernel untouched. Layer 5 will
place this broker behind scoped authority objects. Canonical mutation methods
fail closed unless a private commit gate is supplied.

For M5, navigation is not target authority. Engagement controls are resolved
inside the article that directly owns the approved timestamp link. Submit and
delete bind one concrete DOM control before the gate and revalidate/click that
same marked control after the gate.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
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
    def _cross_commit_gate(commit_gate: Optional[CommitGate]) -> Optional[ActionResult]:
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
        """Resolve the article that directly owns the approved timestamp link."""
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
            "if(a.closest('article')!==arts[ai])continue;"
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
            post_url, testid="bookmark", description="bookmark button"
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
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_like_state(post_url)
        state = (
            (state_r.data or {}).get("like_state", "unknown") if state_r.ok else "unknown"
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
            post_url, testid="like", description="like button"
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
        if (r := self._guard()) is not None:
            return r
        state_r = await self.read_like_state(post_url)
        state = (
            (state_r.data or {}).get("like_state", "unknown") if state_r.ok else "unknown"
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
            post_url, testid="unlike", description="unlike button"
        )
        if not clicked.ok:
            return clicked
        return ok_result(data={"liked": False, "unliked": True})

    @staticmethod
    def _submit_bind_js(token: str, expected_text: str, expected_attachments: int) -> str:
        token_js = json.dumps(token)
        text_js = json.dumps(expected_text)
        return (
            "(function(){"
            f"/*m5-submit-bind:{token}*/var token={token_js};var expected={text_js};"
            f"var expectedCount={expected_attachments};"
            "function visible(el){return !!(el&&el.isConnected&&el.getClientRects().length); }"
            "function countMedia(root){"
            "var att=root.querySelector(\"[data-testid='attachments']\");"
            "if(att){var blobs=att.querySelectorAll(\"img[src*='blob:']\").length;"
            "if(blobs)return blobs;var imgs=att.querySelectorAll('img').length;if(imgs)return imgs;}"
            "return root.querySelectorAll(\"img[src*='blob:']\").length;}"
            "function mediaReady(root){if(expectedCount===0)return true;"
            "var att=root.querySelector(\"[data-testid='attachments']\")||root;"
            "var imgs=att.querySelectorAll('img');var ready=0;"
            "for(var i=0;i<imgs.length;i++){if(imgs[i].complete&&imgs[i].naturalWidth>0)ready++;}"
            "return ready>=expectedCount;}"
            "var tas=document.querySelectorAll(\"[data-testid='tweetTextarea_0']\");"
            "var matches=[];"
            "for(var i=0;i<tas.length;i++){var ta=tas[i];if(!visible(ta))continue;"
            "var root=ta.closest(\"[role='dialog']\")||ta.closest('form');"
            "if(!root){root=ta.parentElement;while(root&&root!==document.body&&"
            "root.querySelectorAll(\"[data-testid='tweetButton']\").length!==1){root=root.parentElement;}}"
            "if(!root||root===document.body)continue;"
            "var btns=root.querySelectorAll(\"[data-testid='tweetButton']\");"
            "if(btns.length!==1||!visible(btns[0]))continue;"
            "if((ta.innerText||'')!==expected)continue;"
            "if(countMedia(root)!==expectedCount||!mediaReady(root))continue;"
            "matches.push([root,ta,btns[0]]); }"
            "if(matches.length!==1)return matches.length?'ambiguous':'missing';"
            "var m=matches[0];m[0].setAttribute('data-wireagent-submit-root',token);"
            "m[2].setAttribute('data-wireagent-submit-button',token);return 'bound';})()"
        )

    @staticmethod
    def _submit_click_js(token: str, expected_text: str, expected_attachments: int) -> str:
        token_js = json.dumps(token)
        text_js = json.dumps(expected_text)
        return (
            "(function(){"
            f"/*m5-submit-click:{token}*/var token={token_js};var expected={text_js};"
            f"var expectedCount={expected_attachments};"
            "function visible(el){return !!(el&&el.isConnected&&el.getClientRects().length); }"
            "function countMedia(root){var att=root.querySelector(\"[data-testid='attachments']\");"
            "if(att){var b=att.querySelectorAll(\"img[src*='blob:']\").length;if(b)return b;"
            "var n=att.querySelectorAll('img').length;if(n)return n;}"
            "return root.querySelectorAll(\"img[src*='blob:']\").length;}"
            "var root=document.querySelector('[data-wireagent-submit-root=\"'+token+'\"]');"
            "var btn=document.querySelector('[data-wireagent-submit-button=\"'+token+'\"]');"
            "if(!visible(root)||!visible(btn)||!root.contains(btn))return 'stale';"
            "var ta=root.querySelector(\"[data-testid='tweetTextarea_0']\");"
            "if(!visible(ta)||(ta.innerText||'')!==expected)return 'payload_changed';"
            "if(countMedia(root)!==expectedCount)return 'attachments_changed';"
            "btn.removeAttribute('data-wireagent-submit-button');"
            "root.removeAttribute('data-wireagent-submit-root');btn.click();return 'clicked';})()"
        )

    async def click_submit(
        self,
        *,
        _commit_gate: Optional[CommitGate] = None,
        _precommit_check: Optional[PrecommitCheck] = None,
        _expected_text: Optional[str] = None,
        _expected_attachments: Optional[int] = None,
    ) -> ActionResult:
        """Bind one verified composer/button, then gate and click that exact button."""
        if (r := self._guard()) is not None:
            return r
        if (
            _precommit_check is None
            or _expected_text is None
            or _expected_attachments is None
            or _expected_attachments < 0
        ):
            return soft_failure(
                "M5 submit requires scoped payload/container verification",
                failure_category=FailureCategory.SECURITY,
            )
        checked = await _precommit_check()
        if checked is not None:
            return checked
        token = secrets.token_hex(16)
        try:
            bound = await self._sb._controller._cdp.evaluate(
                self._submit_bind_js(token, _expected_text, _expected_attachments)
            )
            value = (
                bound.data.get("result", {}).get("value")
                if bound.ok and bound.data
                else None
            )
            if value != "bound":
                return soft_failure(
                    f"submit composer binding failed: {value!r}",
                    failure_category=FailureCategory.SECURITY,
                )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"submit composer binding error: {exc!r}")
        if (r := self._guard()) is not None:
            return r
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        try:
            clicked = await self._sb._controller._cdp.evaluate(
                self._submit_click_js(token, _expected_text, _expected_attachments)
            )
            value = (
                clicked.data.get("result", {}).get("value")
                if clicked.ok and clicked.data
                else None
            )
            if value != "clicked":
                return soft_failure(
                    f"bound submit control changed before click: {value!r}",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"submitted": True})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"bound submit click error: {exc!r}")

    async def close_composer(self) -> ActionResult:
        """Abort cleanup is reducing authority and remains available under kill."""
        try:
            cdp = self._sb._controller._cdp
            result = await cdp.evaluate(
                '(function(){var btns=document.querySelectorAll("button");'
                'for(var i=0;i<btns.length;i++){var a=btns[i].getAttribute("aria-label")||"";'
                'if(a.indexOf("Close")>=0||a.indexOf("Cancel")>=0){btns[i].click();return "closed";}}'
                'return "no_close_button";})()'
            )
            state = (
                result.data.get("result", {}).get("value")
                if result.ok and result.data
                else None
            )
            await asyncio.sleep(1)
            await self._sb.navigate("https://x.com/home", wait_until="domcontentloaded")
            await asyncio.sleep(2)
            return ok_result(data={"cleanup": state or "navigated_away"})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"close_composer error: {exc!r}")

    @staticmethod
    def _delete_article_js(post_id: str, inner: str) -> str:
        target = json.dumps(post_id)
        return (
            "(function(){"
            f"var targetId={target};var arts=document.querySelectorAll('article');"
            "for(var ai=0;ai<arts.length;ai++){var art=arts[ai];"
            "var links=art.querySelectorAll('a[href]');var owned=false;"
            "for(var li=0;li<links.length;li++){var a=links[li];"
            "if(a.closest('article')!==art||!a.querySelector('time'))continue;"
            "try{var u=new URL(a.href,location.href);"
            "if(u.pathname.endsWith('/status/'+targetId)){owned=true;break;}}catch(e){}}"
            "if(!owned)continue;" + inner + "}return null;})()"
        )

    @staticmethod
    def _delete_baseline_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"/*m5-delete-baseline:{token}*/var token={token_js};"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length); }"
            "var ms=document.querySelectorAll(\"[data-testid='Dropdown'],[role='menu']\");"
            "for(var i=0;i<ms.length;i++){if(vis(ms[i]))ms[i].setAttribute('data-wireagent-delete-baseline',token);}"
            "var cs=document.querySelectorAll(\"[data-testid='confirmationSheetConfirm']\");"
            "for(var j=0;j<cs.length;j++){if(vis(cs[j]))cs[j].setAttribute('data-wireagent-delete-baseline',token);}"
            "return 'baselined';})()"
        )

    @staticmethod
    def _delete_bind_menu_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"/*m5-delete-bind-menu:{token}*/var token={token_js};"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length); }"
            "var ms=document.querySelectorAll(\"[data-testid='Dropdown'],[role='menu']\");var found=[];"
            "for(var m=0;m<ms.length;m++){if(!vis(ms[m]))continue;"
            "if(ms[m].getAttribute('data-wireagent-delete-baseline')===token)continue;"
            "var items=ms[m].querySelectorAll(\"[role='menuitem'],a,button\");"
            "for(var i=0;i<items.length;i++){var t=(items[i].innerText||'').trim();"
            "if(t==='Delete'||t==='Delete post'||t==='删除'||t==='删除帖子'){found.push([ms[m],items[i]]);break;}}}"
            "if(found.length!==1)return null;found[0][0].setAttribute('data-wireagent-delete-menu',token);"
            "found[0][1].setAttribute('data-wireagent-delete-item',token);return 'menu_bound';})()"
        )

    @staticmethod
    def _delete_click_menu_item_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"/*m5-delete-click-menu:{token}*/var token={token_js};"
            "var menu=document.querySelector('[data-wireagent-delete-menu=\"'+token+'\"]');"
            "var item=document.querySelector('[data-wireagent-delete-item=\"'+token+'\"]');"
            "if(!menu||!item||!menu.contains(item)||!item.isConnected)return null;"
            "item.removeAttribute('data-wireagent-delete-item');item.click();return 'delete_item_clicked';})()"
        )

    @staticmethod
    def _delete_bind_confirm_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"/*m5-delete-bind-confirm:{token}*/var token={token_js};"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length); }"
            "var found=[];var cs=document.querySelectorAll(\"[data-testid='confirmationSheetConfirm']\");"
            "for(var i=0;i<cs.length;i++){if(vis(cs[i])&&cs[i].getAttribute('data-wireagent-delete-baseline')!==token)found.push(cs[i]);}"
            "if(!found.length){var ds=document.querySelectorAll(\"[role='dialog']\");"
            "for(var d=0;d<ds.length;d++){if(!vis(ds[d]))continue;var bs=ds[d].querySelectorAll('button');"
            "for(var b=0;b<bs.length;b++){if((bs[b].innerText||'').trim()==='Delete'){found.push(bs[b]);break;}}}}"
            "if(found.length!==1)return null;found[0].setAttribute('data-wireagent-delete-confirm',token);return 'confirm_bound';})()"
        )

    @staticmethod
    def _delete_click_confirm_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"/*m5-delete-click-confirm:{token}*/var token={token_js};"
            "var b=document.querySelector('[data-wireagent-delete-confirm=\"'+token+'\"]');"
            "if(!b||!b.isConnected||!b.getClientRects().length)return null;"
            "b.removeAttribute('data-wireagent-delete-confirm');b.click();return 'confirm_clicked';})()"
        )

    async def delete_post(
        self,
        post_url: str,
        post_id: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        """Target-scoped delete; mint/consume immediately before bound confirm click."""
        if (r := self._guard()) is not None:
            return r
        if self._status_id(post_url) != post_id:
            return soft_failure(
                "delete_post target URL/id mismatch",
                failure_category=FailureCategory.SECURITY,
            )
        token = secrets.token_hex(16)
        try:
            nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
            if not nav.ok:
                return nav
            await self._delete_eval(self._delete_baseline_js(token))

            target = await self._delete_poll(
                lambda: self._delete_eval(
                    self._delete_article_js(post_id, 'return "found";')
                ),
                want_true=True,
                label="target article",
            )
            if not target.ok:
                return soft_failure(
                    "delete_post: approved target article not found",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            caret = await self._delete_eval(
                self._delete_article_js(
                    post_id,
                    'var c=art.querySelector("[data-testid=\'caret\']");'
                    'if(!c)return null;c.click();return "caret_clicked";',
                )
            )
            if not (caret.ok and caret.data):
                return soft_failure(
                    "delete_post: caret button not found on approved target",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            menu = await self._delete_poll(
                lambda: self._delete_eval(self._delete_bind_menu_js(token)),
                want_true=True,
                label="bound delete menu",
            )
            if not menu.ok:
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: unique target-triggered Delete menu not found",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            menu_click = await self._delete_eval(self._delete_click_menu_item_js(token))
            if not (menu_click.ok and menu_click.data):
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: bound Delete item disappeared before staging click",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            confirm = await self._delete_poll(
                lambda: self._delete_eval(self._delete_bind_confirm_js(token)),
                want_true=True,
                label="bound confirmation button",
            )
            if not confirm.ok:
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: unique confirmation control not found",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            if (r := self._guard()) is not None:
                await self._dismiss_delete_dialog()
                return r
            if (denied := self._cross_commit_gate(_commit_gate)) is not None:
                await self._dismiss_delete_dialog()
                return denied

            clicked = await self._delete_eval(self._delete_click_confirm_js(token))
            if not (clicked.ok and clicked.data):
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: bound confirmation control disappeared before click",
                    failure_category=FailureCategory.UNKNOWN,
                )

            dismissed = await self._delete_poll(
                lambda: self._delete_eval(
                    "(function(){return document.querySelector("
                    f"'[data-wireagent-delete-confirm=\"{token}\"]')===null;}})()"
                ),
                want_true=True,
                label="confirmation sheet dismissed",
            )
            return ok_result(
                data={
                    "deleted": True,
                    "post_id": post_id,
                    "sheet_dismissed": bool(dismissed.ok),
                }
            )
        except Exception as exc:  # noqa: BLE001
            await self._dismiss_delete_dialog()
            return soft_failure(f"delete_post error: {exc!r}")
