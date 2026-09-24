"""Browser-session lease for M5 scoped write operations.

``M5ScopedWriteBroker`` proves DOM provenance inside one operation. This wrapper
adds the missing same-process browser-session coordination needed when multiple
M5 write attempts share one ``SuperBrowser`` instance:

- one async write operation at a time per underlying browser;
- one active content-composer owner at a time per browser;
- target-click -> transient-context binding cannot interleave with another M5
  writer;
- plain-post composer binding happens before any approved text is typed;
- focus -> keyboard typing and upload -> preview binding stay inside the lease;
- one-step engagement/delete cannot navigate over an owned content context;
- delete menu and confirmation controls are causally bound to the approved target.

This is still an engineering boundary, not a hostile-code sandbox. Layer 5 must
ensure legacy/read paths do not concurrently navigate the same browser while an
M5 scoped write owns it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
import time
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_scoped_write_broker import M5ScopedWriteBroker
from webwire.m5_write_broker import CommitGate, PrecommitCheck

__all__ = ["M5LeasedWriteBroker"]


class _BrowserWriteState:
    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.content_owner: Optional[str] = None


_STATE_CREATE_LOCK = threading.RLock()
_STATE_ATTR = "_wireagent_m5_write_state"


def _browser_state(sb: Any) -> _BrowserWriteState:
    """Return the process-local M5 write state attached to one browser facade."""
    with _STATE_CREATE_LOCK:
        existing = getattr(sb, _STATE_ATTR, None)
        if isinstance(existing, _BrowserWriteState):
            return existing
        state = _BrowserWriteState()
        setattr(sb, _STATE_ATTR, state)
        return state


class M5LeasedWriteBroker(M5ScopedWriteBroker):
    """Provenance broker plus per-browser M5 write serialization."""

    scoped_authority_version = 3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._m5_lease_owner = secrets.token_hex(16)
        self._m5_write_state = _browser_state(self._sb)

    @staticmethod
    def _busy(detail: str) -> ActionResult:
        return soft_failure(detail, failure_category=FailureCategory.SECURITY)

    def _claim_content_owner(self) -> Optional[ActionResult]:
        owner = self._m5_write_state.content_owner
        if owner is None:
            self._m5_write_state.content_owner = self._m5_lease_owner
            return None
        if owner == self._m5_lease_owner:
            return self._busy("this M5 broker already owns an active composer context")
        return self._busy("another M5 write owns the browser composer context")

    def _require_content_owner(self) -> Optional[ActionResult]:
        if self._m5_write_state.content_owner != self._m5_lease_owner:
            return self._busy("M5 composer context is not owned by this write attempt")
        return None

    def _release_content_owner(self) -> None:
        if self._m5_write_state.content_owner == self._m5_lease_owner:
            self._m5_write_state.content_owner = None

    def _clear_context(self) -> None:
        self._m5_context_token = None
        self._m5_context_kind = None
        self._m5_context_target = None

    async def _start_content(
        self,
        operation: Callable[[], Awaitable[ActionResult]],
    ) -> ActionResult:
        async with self._m5_write_state.lock:
            if (blocked := self._claim_content_owner()) is not None:
                return blocked
            try:
                result = await operation()
            except BaseException:
                if self._m5_context_token is None:
                    self._clear_context()
                    self._release_content_owner()
                raise
            if self._m5_context_token is None:
                self._clear_context()
                self._release_content_owner()
            return result

    async def _owned_content(
        self,
        operation: Callable[[], Awaitable[ActionResult]],
    ) -> ActionResult:
        async with self._m5_write_state.lock:
            if (blocked := self._require_content_owner()) is not None:
                return blocked
            return await operation()

    async def _one_shot(
        self,
        operation: Callable[[], Awaitable[ActionResult]],
    ) -> ActionResult:
        async with self._m5_write_state.lock:
            if self._m5_write_state.content_owner is not None:
                return self._busy("browser is reserved by an active M5 content context")
            return await operation()

    async def _verify_bound_text(self, expected: str) -> ActionResult:
        context = self._require_context()
        if context is None:
            return self._busy("approved composer context disappeared after typing")
        token, _, _ = context
        expr = (
            "(function(){"
            f"var token={json.dumps(token)},expected={json.dumps(expected)};"
            "var root=document.querySelector('[data-wireagent-context=\"'+token+'\"]');"
            "if(!root||!root.isConnected)return 'stale';"
            "var ta=root.querySelector(\"[data-testid='tweetTextarea_0']\");"
            "if(!ta)return 'missing';return (ta.innerText||'')===expected?'match':'mismatch';})()"
        )
        result = await self._sb._controller._cdp.evaluate(expr)
        value = result.data.get("result", {}).get("value") if result.ok and result.data else None
        if value != "match":
            return soft_failure(
                f"approved composer text verification failed: {value!r}",
                failure_category=FailureCategory.SECURITY,
            )
        return ok_result(data={"context_text_verified": True})

    @staticmethod
    def _bind_empty_plain_composer_js(context_token: str) -> str:
        token = json.dumps(context_token)
        return (
            "(function(){"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"
            f"var token={token};"
            "var tas=document.querySelectorAll(\"[data-testid='tweetTextarea_0']\");"
            "var found=[];"
            "for(var i=0;i<tas.length;i++){var ta=tas[i];if(!vis(ta))continue;"
            "var root=ta.closest(\"[role='dialog']\")||ta.closest('form');"
            "if(!root){root=ta.parentElement;while(root&&root!==document.body&&"
            "root.querySelectorAll(\"[data-testid='tweetButton']\").length!==1)"
            "{root=root.parentElement;}}"
            "if(!root||root===document.body)continue;found.push([root,ta]);}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "var root=found[0][0],ta=found[0][1];"
            "if((ta.innerText||'').trim()!=='')return 'nonempty';"
            "root.setAttribute('data-wireagent-context',token);"
            "root.setAttribute('data-wireagent-context-kind','post');"
            "root.setAttribute('data-wireagent-context-target','none');"
            "var imgs=root.querySelectorAll('img');"
            "for(var j=0;j<imgs.length;j++)"
            "imgs[j].setAttribute('data-wireagent-context-preexisting',token);"
            "return 'bound';})()"
        )

    async def _open_empty_plain_composer(self, text: str) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        nav = await self._sb.navigate(
            "https://x.com/compose/post",
            wait_until="domcontentloaded",
        )
        if not nav.ok:
            return nav
        context_token = secrets.token_hex(16)
        cdp = self._sb._controller._cdp
        for _ in range(20):
            bound = await cdp.evaluate(self._bind_empty_plain_composer_js(context_token))
            value = bound.data.get("result", {}).get("value") if bound.ok and bound.data else None
            if value == "bound":
                self._m5_context_token = context_token
                self._m5_context_kind = "post"
                self._m5_context_target = "none"
                break
            if value in {"ambiguous", "nonempty"}:
                return soft_failure(
                    f"plain-post composer is not uniquely empty: {value!r}",
                    failure_category=FailureCategory.SECURITY,
                )
            await asyncio.sleep(0.25)
        else:
            return soft_failure(
                "plain-post composer did not appear",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        focused = await self._focus_bound_textarea()
        if not focused.ok:
            return focused
        if (r := self._guard()) is not None:
            return r
        try:
            await self._sb._page.engine_page.backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(0.25)
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_composer error: {exc!r}")
        return await self._verify_bound_text(text)

    @staticmethod
    def _baseline_delete_menus_js(baseline: str) -> str:
        mark = json.dumps(baseline)
        return (
            "(function(){"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"
            f"var mark={mark};"
            "var menus=document.querySelectorAll(\"[data-testid='Dropdown'],[role='menu']\");"
            "for(var i=0;i<menus.length;i++)if(vis(menus[i]))"
            "menus[i].setAttribute('data-wireagent-delete-menu-baseline',mark);"
            "return 'baselined';})()"
        )

    @staticmethod
    def _bind_new_delete_menu_js(baseline: str, token: str) -> str:
        base = json.dumps(baseline)
        mark = json.dumps(token)
        return (
            "(function(){"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"
            f"var base={base},mark={mark};"
            "var menus=document.querySelectorAll(\"[data-testid='Dropdown'],[role='menu']\");"
            "var found=[];"
            "for(var m=0;m<menus.length;m++){var menu=menus[m];if(!vis(menu))continue;"
            "if(menu.getAttribute('data-wireagent-delete-menu-baseline')===base)continue;"
            "var items=menu.querySelectorAll(\"[role='menuitem'],a,button\");"
            "for(var i=0;i<items.length;i++){var t=(items[i].innerText||'').trim();"
            "if(t==='Delete'||t==='Delete post'||t==='删除'||t==='删除帖子')"
            "{found.push([menu,items[i]]);break;}}}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "found[0][0].setAttribute('data-wireagent-delete-menu',mark);"
            "found[0][1].setAttribute('data-wireagent-delete-item',mark);"
            "return 'bound';})()"
        )

    @staticmethod
    def _click_bound_delete_item_js(token: str) -> str:
        mark = json.dumps(token)
        return (
            "(function(){"
            f"var mark={mark};"
            "var menu=document.querySelector('[data-wireagent-delete-menu=\"'+mark+'\"]');"
            "var item=document.querySelector('[data-wireagent-delete-item=\"'+mark+'\"]');"
            "if(!menu||!item||!menu.contains(item)||!item.isConnected)return 'stale';"
            "item.removeAttribute('data-wireagent-delete-item');item.click();return 'clicked';})()"
        )

    @staticmethod
    def _baseline_delete_confirms_js(baseline: str) -> str:
        mark = json.dumps(baseline)
        return (
            "(function(){"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"
            f"var mark={mark};"
            "var bs=document.querySelectorAll(\"[data-testid='confirmationSheetConfirm']\");"
            "for(var i=0;i<bs.length;i++)if(vis(bs[i]))"
            "bs[i].setAttribute('data-wireagent-delete-confirm-baseline',mark);"
            "var dialogs=document.querySelectorAll(\"[role='dialog']\");"
            "for(var d=0;d<dialogs.length;d++){if(!vis(dialogs[d]))continue;"
            "var buttons=dialogs[d].querySelectorAll('button');"
            "for(var j=0;j<buttons.length;j++){if(vis(buttons[j])&&"
            "(buttons[j].innerText||'').trim()==='Delete')"
            "buttons[j].setAttribute('data-wireagent-delete-confirm-baseline',mark);}}"
            "return 'baselined';})()"
        )

    @staticmethod
    def _bind_new_delete_confirm_js(baseline: str, token: str) -> str:
        base = json.dumps(baseline)
        mark = json.dumps(token)
        return (
            "(function(){"
            "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"
            f"var base={base},mark={mark};var found=[];"
            "var primary=document.querySelectorAll(\"[data-testid='confirmationSheetConfirm']\");"
            "for(var i=0;i<primary.length;i++){var b=primary[i];if(!vis(b))continue;"
            "if(b.getAttribute('data-wireagent-delete-confirm-baseline')===base)continue;"
            "found.push(b);}"
            "if(found.length===0){var dialogs=document.querySelectorAll(\"[role='dialog']\");"
            "for(var d=0;d<dialogs.length;d++){if(!vis(dialogs[d]))continue;"
            "var buttons=dialogs[d].querySelectorAll('button');"
            "for(var j=0;j<buttons.length;j++){var b=buttons[j];"
            "if(!vis(b)||(b.innerText||'').trim()!=='Delete')continue;"
            "if(b.getAttribute('data-wireagent-delete-confirm-baseline')===base)continue;"
            "found.push(b);}}}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "found[0].setAttribute('data-wireagent-delete-confirm',mark);return 'bound';})()"
        )

    @staticmethod
    def _click_bound_delete_confirm_js(token: str) -> str:
        mark = json.dumps(token)
        return (
            "(function(){"
            f"var mark={mark};"
            "var b=document.querySelector('[data-wireagent-delete-confirm=\"'+mark+'\"]');"
            "if(!b||!b.isConnected||!b.getClientRects().length)return 'stale';"
            "b.removeAttribute('data-wireagent-delete-confirm');b.click();return 'clicked';})()"
        )

    async def _poll_delete_binding(self, expr: str, *, label: str) -> ActionResult:
        deadline = time.monotonic() + self._DELETE_STAGE_TIMEOUT_S
        while True:
            result = await self._delete_eval(expr)
            if result.ok and result.data == "bound":
                return result
            if result.ok and result.data == "ambiguous":
                return soft_failure(
                    f"delete binding ambiguous: {label}",
                    failure_category=FailureCategory.SECURITY,
                )
            if time.monotonic() >= deadline:
                return soft_failure(
                    f"delete binding timeout: {label}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            await asyncio.sleep(self._DELETE_POLL_INTERVAL_S)

    async def fill_composer(self, text: str) -> ActionResult:
        return await self._start_content(lambda: self._open_empty_plain_composer(text))

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        return await self._start_content(
            lambda: super(M5LeasedWriteBroker, self).open_reply_on_target(post_url, target_post_id)
        )

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        return await self._start_content(
            lambda: super(M5LeasedWriteBroker, self).open_quote_on_target(post_url, target_post_id)
        )

    async def fill_reply_composer(self, text: str) -> ActionResult:
        async def operation() -> ActionResult:
            result = await super(M5LeasedWriteBroker, self).fill_reply_composer(text)
            if not result.ok:
                return result
            return await self._verify_bound_text(text)

        return await self._owned_content(operation)

    async def fill_quote_composer(self, text: str) -> ActionResult:
        async def operation() -> ActionResult:
            result = await super(M5LeasedWriteBroker, self).fill_quote_composer(text)
            if not result.ok:
                return result
            return await self._verify_bound_text(text)

        return await self._owned_content(operation)

    async def attach_media(self, image_path: str) -> ActionResult:
        return await self._owned_content(
            lambda: super(M5LeasedWriteBroker, self).attach_media(image_path)
        )

    async def close_composer(self) -> ActionResult:
        async with self._m5_write_state.lock:
            if (blocked := self._require_content_owner()) is not None:
                return blocked
            result = await super().close_composer()
            if result.ok:
                self._clear_context()
                self._release_content_owner()
            return result

    async def click_submit(
        self,
        *,
        _commit_gate: Optional[CommitGate] = None,
        _precommit_check: Optional[PrecommitCheck] = None,
        _expected_text: Optional[str] = None,
        _expected_attachments: Optional[int] = None,
    ) -> ActionResult:
        async with self._m5_write_state.lock:
            if (blocked := self._require_content_owner()) is not None:
                return blocked
            crossed = False

            def gate() -> Optional[ActionResult]:
                nonlocal crossed
                if _commit_gate is None:
                    return self._cross_commit_gate(None)
                denied = _commit_gate()
                if denied is None:
                    crossed = True
                return denied

            result = await super().click_submit(
                _commit_gate=gate,
                _precommit_check=_precommit_check,
                _expected_text=_expected_text,
                _expected_attachments=_expected_attachments,
            )
            if crossed:
                self._clear_context()
                self._release_content_owner()
            return result

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: super(M5LeasedWriteBroker, self).click_bookmark(
                post_url, _commit_gate=_commit_gate
            )
        )

    async def click_remove_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: super(M5LeasedWriteBroker, self).click_remove_bookmark(
                post_url, _commit_gate=_commit_gate
            )
        )

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: super(M5LeasedWriteBroker, self).click_like(
                post_url, _commit_gate=_commit_gate
            )
        )

    async def click_unlike(
        self,
        post_url: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: super(M5LeasedWriteBroker, self).click_unlike(
                post_url, _commit_gate=_commit_gate
            )
        )

    async def _delete_scoped(
        self,
        post_url: str,
        post_id: str,
        commit_gate: Optional[CommitGate],
    ) -> ActionResult:
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

            found = await self._delete_poll(
                lambda: self._delete_eval(self._delete_article_js(post_id, 'return "found";')),
                want_true=True,
                label="target article",
            )
            if not found.ok:
                return soft_failure(
                    "delete_post: target post not found",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            cdp = self._sb._controller._cdp
            menu_baseline = secrets.token_hex(16)
            menu_baseline_result = await cdp.evaluate(self._baseline_delete_menus_js(menu_baseline))
            menu_baselined = self._require_baselined(menu_baseline_result, "existing delete menus")
            if not menu_baselined.ok:
                return menu_baselined
            caret = await self._delete_eval(
                self._delete_article_js(
                    post_id,
                    'var c=art.querySelector("[data-testid=\'caret\']");'
                    'if(!c)return null;c.click();return "caret_clicked";',
                )
            )
            if not (caret.ok and caret.data):
                return soft_failure(
                    "delete_post: caret button not found on target article",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )

            menu_token = secrets.token_hex(16)
            menu = await self._poll_delete_binding(
                self._bind_new_delete_menu_js(menu_baseline, menu_token),
                label="target-triggered delete menu",
            )
            if not menu.ok:
                return menu

            confirm_baseline = secrets.token_hex(16)
            confirm_baseline_result = await cdp.evaluate(
                self._baseline_delete_confirms_js(confirm_baseline)
            )
            confirm_baselined = self._require_baselined(
                confirm_baseline_result, "existing delete confirmations"
            )
            if not confirm_baselined.ok:
                await self._dismiss_delete_dialog()
                return confirm_baselined
            clicked_item = await self._delete_eval(self._click_bound_delete_item_js(menu_token))
            if not (clicked_item.ok and clicked_item.data):
                return soft_failure(
                    "delete_post: bound Delete menu item became stale",
                    failure_category=FailureCategory.UNKNOWN,
                )

            confirm_token = secrets.token_hex(16)
            confirm = await self._poll_delete_binding(
                self._bind_new_delete_confirm_js(confirm_baseline, confirm_token),
                label="target-triggered delete confirmation",
            )
            if not confirm.ok:
                await self._dismiss_delete_dialog()
                return confirm

            if (r := self._guard()) is not None:
                await self._dismiss_delete_dialog()
                return r
            if (denied := self._cross_commit_gate(commit_gate)) is not None:
                await self._dismiss_delete_dialog()
                return denied

            clicked_confirm = await self._delete_eval(
                self._click_bound_delete_confirm_js(confirm_token)
            )
            if not (clicked_confirm.ok and clicked_confirm.data):
                await self._dismiss_delete_dialog()
                return soft_failure(
                    "delete_post: bound confirmation changed after authority crossing",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"deleted": True, "post_id": post_id})
        except Exception as exc:  # noqa: BLE001
            await self._dismiss_delete_dialog()
            return soft_failure(f"delete_post error: {exc!r}")

    async def delete_post(
        self,
        post_url: str,
        post_id: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: self._delete_scoped(post_url, post_id, _commit_gate)
        )
