"""Provenance-hardened M5 broker for scoped content authority.

``M5WriteBroker`` owns the exact canonical mutation seams. This subclass adds
browser-side provenance for reversible content staging so Layer 4 can prove that
the final submit belongs to the approved composer context and to the previews
created by approved uploads.

The provenance markers are a same-process engineering mechanism, not a
cryptographic statement about X's remote media object. Local media bytes are
SHA-256 checked by ``PreparationAuthority`` immediately before upload. This
broker then marks the exact new preview node produced by that upload and fails
closed if X later replaces or re-renders that node.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_write_broker import CommitGate, M5WriteBroker, PrecommitCheck

__all__ = ["M5ScopedWriteBroker"]


class M5ScopedWriteBroker(M5WriteBroker):
    """M5 broker whose content staging is bound to one DOM composer context."""

    scoped_authority_version = 2

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._m5_context_token: Optional[str] = None
        self._m5_context_kind: Optional[str] = None
        self._m5_context_target: Optional[str] = None

    @staticmethod
    def _visible_js() -> str:
        return "function vis(e){return !!(e&&e.isConnected&&e.getClientRects().length);}"

    @staticmethod
    def _require_baselined(result: ActionResult, label: str) -> ActionResult:
        """Accept provenance exclusion only when the baseline is explicitly proven."""
        value = (
            result.data.get("result", {}).get("value")
            if result.ok and result.data and "exceptionDetails" not in result.data
            else None
        )
        if value == "baselined":
            return ok_result(data={"baselined": True})
        return soft_failure(
            f"could not establish provenance baseline: {label} ({value!r})",
            failure_category=FailureCategory.SECURITY,
        )

    @staticmethod
    def _composer_root_js(textarea_var: str = "ta") -> str:
        return (
            f"var root={textarea_var}.closest(\"[role='dialog']\")||{textarea_var}.closest('form');"
            f"if(!root){{root={textarea_var}.parentElement;"
            "while(root&&root!==document.body&&"
            "root.querySelectorAll(\"[data-testid='tweetButton']\").length!==1)"
            "{root=root.parentElement;}}"
        )

    @classmethod
    def _baseline_composers_js(cls, baseline: str) -> str:
        mark = json.dumps(baseline)
        return (
            "(function(){"
            + cls._visible_js()
            + f"var mark={mark};var tas=document.querySelectorAll(\"[data-testid='tweetTextarea_0']\");"
            "for(var i=0;i<tas.length;i++){var ta=tas[i];if(!vis(ta))continue;"
            + cls._composer_root_js("ta")
            + "if(root&&root!==document.body)root.setAttribute('data-wireagent-context-baseline',mark);}"
            "return 'baselined';})()"
        )

    @classmethod
    def _bind_composer_js(
        cls,
        *,
        baseline: str,
        context_token: str,
        kind: str,
        target_id: str,
        expected_text: Optional[str] = None,
    ) -> str:
        base = json.dumps(baseline)
        token = json.dumps(context_token)
        kind_js = json.dumps(kind)
        target = json.dumps(target_id)
        text_check = ""
        if expected_text is not None:
            text_check = f"if((ta.innerText||'')!=={json.dumps(expected_text)})continue;"
        return (
            "(function(){"
            + cls._visible_js()
            + f"var base={base},token={token},kind={kind_js},target={target};"
            "var tas=document.querySelectorAll(\"[data-testid='tweetTextarea_0']\");var found=[];"
            "for(var i=0;i<tas.length;i++){var ta=tas[i];if(!vis(ta))continue;"
            + text_check
            + cls._composer_root_js("ta")
            + "if(!root||root===document.body)continue;"
            "if(root.getAttribute('data-wireagent-context-baseline')===base)continue;"
            "found.push(root);}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "var root=found[0];root.setAttribute('data-wireagent-context',token);"
            "root.setAttribute('data-wireagent-context-kind',kind);"
            "root.setAttribute('data-wireagent-context-target',target);"
            "var imgs=root.querySelectorAll('img');"
            "for(var j=0;j<imgs.length;j++)imgs[j].setAttribute('data-wireagent-context-preexisting',token);"
            "return 'bound';})()"
        )

    async def _bind_new_context(
        self,
        *,
        baseline: str,
        kind: str,
        target_id: str,
        expected_text: Optional[str] = None,
    ) -> ActionResult:
        context = secrets.token_hex(16)
        expr = self._bind_composer_js(
            baseline=baseline,
            context_token=context,
            kind=kind,
            target_id=target_id,
            expected_text=expected_text,
        )
        cdp = self._sb._controller._cdp
        for _ in range(8):
            result = await cdp.evaluate(expr)
            value = result.data.get("result", {}).get("value") if result.ok and result.data else None
            if value == "bound":
                self._m5_context_token = context
                self._m5_context_kind = kind
                self._m5_context_target = target_id
                return ok_result(data={"context_bound": True, "kind": kind, "target_id": target_id})
            if value == "ambiguous":
                return soft_failure(
                    "multiple new composer contexts appeared",
                    failure_category=FailureCategory.SECURITY,
                )
            await asyncio.sleep(0.25)
        return soft_failure(
            "approved composer context did not appear",
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
        )

    async def _baseline_composers(self, baseline: str) -> ActionResult:
        result = await self._sb._controller._cdp.evaluate(self._baseline_composers_js(baseline))
        return self._require_baselined(result, "existing composer contexts")

    def _require_context(self) -> tuple[str, str, str] | None:
        if not self._m5_context_token or not self._m5_context_kind:
            return None
        return (self._m5_context_token, self._m5_context_kind, self._m5_context_target or "")

    async def fill_composer(self, text: str) -> ActionResult:
        """Open/fill a plain-post composer and bind exactly that context."""
        if (r := self._guard()) is not None:
            return r
        baseline = secrets.token_hex(16)
        baseline_result = await self._baseline_composers(baseline)
        if not baseline_result.ok:
            return baseline_result
        result = await super().fill_composer(text)
        if not result.ok:
            return result
        return await self._bind_new_context(
            baseline=baseline,
            kind="post",
            target_id="none",
            expected_text=text,
        )

    @staticmethod
    def _target_click_js(post_id: str, testid: str, marker: str) -> str:
        return (
            M5WriteBroker._target_article_prefix(post_id, marker)
            + f"var b=art.querySelector(\"[data-testid='{testid}']\");"
            + "if(!b)return 'control_missing';b.click();return 'clicked';})()"
        )

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        if self._status_id(post_url) != target_post_id:
            return soft_failure("reply target URL/id mismatch", failure_category=FailureCategory.SECURITY)
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(0.5)
        baseline = secrets.token_hex(16)
        baseline_result = await self._baseline_composers(baseline)
        if not baseline_result.ok:
            return baseline_result
        clicked = await self._sb._controller._cdp.evaluate(
            self._target_click_js(target_post_id, "reply", "m5-context-reply")
        )
        value = clicked.data.get("result", {}).get("value") if clicked.ok and clicked.data else None
        if value != "clicked":
            return soft_failure(
                f"reply control unavailable on approved target {target_post_id!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        return await self._bind_new_context(baseline=baseline, kind="reply", target_id=target_post_id)

    @staticmethod
    def _baseline_menus_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            + M5ScopedWriteBroker._visible_js()
            + f"var token={token_js};var ms=document.querySelectorAll(\"[role='menu']\");"
            "for(var i=0;i<ms.length;i++)if(vis(ms[i]))"
            "ms[i].setAttribute('data-wireagent-menu-baseline',token);"
            "return 'baselined';})()"
        )

    @staticmethod
    def _bind_quote_menu_js(baseline: str, token: str) -> str:
        base = json.dumps(baseline)
        token_js = json.dumps(token)
        return (
            "(function(){"
            + M5ScopedWriteBroker._visible_js()
            + f"var base={base},token={token_js};"
            "var ms=document.querySelectorAll(\"[role='menu']\");var found=[];"
            "for(var m=0;m<ms.length;m++){var menu=ms[m];if(!vis(menu))continue;"
            "if(menu.getAttribute('data-wireagent-menu-baseline')===base)continue;"
            "var items=menu.querySelectorAll(\"[role='menuitem']\");"
            "for(var i=0;i<items.length;i++){if((items[i].innerText||'').trim()==='Quote')"
            "{found.push([menu,items[i]]);break;}}}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "found[0][0].setAttribute('data-wireagent-quote-menu',token);"
            "found[0][1].setAttribute('data-wireagent-quote-item',token);return 'bound';})()"
        )

    @staticmethod
    def _click_bound_quote_js(token: str) -> str:
        token_js = json.dumps(token)
        return (
            "(function(){"
            f"var token={token_js};var menu=document.querySelector('[data-wireagent-quote-menu=\"'+token+'\"]');"
            "var item=document.querySelector('[data-wireagent-quote-item=\"'+token+'\"]');"
            "if(!menu||!item||!menu.contains(item)||!item.isConnected)return 'stale';"
            "item.removeAttribute('data-wireagent-quote-item');item.click();return 'clicked';})()"
        )

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        if self._status_id(post_url) != target_post_id:
            return soft_failure("quote target URL/id mismatch", failure_category=FailureCategory.SECURITY)
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(0.5)
        composer_baseline = secrets.token_hex(16)
        menu_baseline = secrets.token_hex(16)
        composer_result = await self._baseline_composers(composer_baseline)
        if not composer_result.ok:
            return composer_result
        menu_result = await self._sb._controller._cdp.evaluate(self._baseline_menus_js(menu_baseline))
        menu_baselined = self._require_baselined(menu_result, "existing quote menus")
        if not menu_baselined.ok:
            return menu_baselined
        repost = await self._sb._controller._cdp.evaluate(
            self._target_click_js(target_post_id, "retweet", "m5-context-quote")
        )
        repost_value = repost.data.get("result", {}).get("value") if repost.ok and repost.data else None
        if repost_value != "clicked":
            return soft_failure(
                f"repost control unavailable on approved target {target_post_id!r}",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        quote_token = secrets.token_hex(16)
        cdp = self._sb._controller._cdp
        for _ in range(8):
            bound = await cdp.evaluate(self._bind_quote_menu_js(menu_baseline, quote_token))
            value = bound.data.get("result", {}).get("value") if bound.ok and bound.data else None
            if value == "bound":
                break
            if value == "ambiguous":
                return soft_failure("multiple new quote menus appeared", failure_category=FailureCategory.SECURITY)
            await asyncio.sleep(0.25)
        else:
            return soft_failure(
                "target-triggered Quote menu did not appear",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        clicked = await cdp.evaluate(self._click_bound_quote_js(quote_token))
        click_value = clicked.data.get("result", {}).get("value") if clicked.ok and clicked.data else None
        if click_value != "clicked":
            return soft_failure("bound Quote menu item became stale", failure_category=FailureCategory.UNKNOWN)
        return await self._bind_new_context(
            baseline=composer_baseline,
            kind="quote",
            target_id=target_post_id,
        )

    async def _focus_bound_textarea(self) -> ActionResult:
        context = self._require_context()
        if context is None:
            return soft_failure("no approved composer context", failure_category=FailureCategory.SECURITY)
        token, _, _ = context
        expr = (
            "(function(){"
            f"var token={json.dumps(token)};var root=document.querySelector('[data-wireagent-context=\"'+token+'\"]');"
            "if(!root||!root.isConnected)return 'stale';"
            "var ta=root.querySelector(\"[data-testid='tweetTextarea_0']\");"
            "if(!ta)return 'missing';ta.focus();ta.click();return 'focused';})()"
        )
        result = await self._sb._controller._cdp.evaluate(expr)
        value = result.data.get("result", {}).get("value") if result.ok and result.data else None
        if value != "focused":
            return soft_failure(
                "approved composer textarea unavailable",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        return ok_result(data={"focused": True})

    async def fill_reply_composer(self, text: str) -> ActionResult:
        context = self._require_context()
        if context is None or context[1] != "reply":
            return soft_failure("reply composer context is not bound", failure_category=FailureCategory.SECURITY)
        focused = await self._focus_bound_textarea()
        if not focused.ok:
            return focused
        try:
            await self._sb._page.engine_page.backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(0.25)
            return ok_result(data={"filled": True, "text_length": len(text)})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_reply_composer error: {exc!r}")

    async def fill_quote_composer(self, text: str) -> ActionResult:
        context = self._require_context()
        if context is None or context[1] != "quote":
            return soft_failure("quote composer context is not bound", failure_category=FailureCategory.SECURITY)
        focused = await self._focus_bound_textarea()
        if not focused.ok:
            return focused
        try:
            await self._sb._page.engine_page.backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(0.25)
            return ok_result(data={"filled": True, "text_length": len(text)})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_quote_composer error: {exc!r}")

    @staticmethod
    def _mark_media_input_js(context_token: str, input_token: str) -> str:
        context = json.dumps(context_token)
        token = json.dumps(input_token)
        return (
            "(function(){"
            f"var context={context},token={token};"
            "var root=document.querySelector('[data-wireagent-context=\"'+context+'\"]');"
            "if(!root||!root.isConnected)return 'stale';"
            "var inputs=root.querySelectorAll(\"input[type='file']\");"
            "if(inputs.length===0){var form=root.closest('form')||root.querySelector('form');"
            "if(form)inputs=form.querySelectorAll(\"input[type='file']\");}"
            "if(inputs.length!==1)return inputs.length?'ambiguous':'missing';"
            "inputs[0].setAttribute('data-wireagent-media-input',token);return 'bound';})()"
        )

    @staticmethod
    def _baseline_context_images_js(context_token: str, baseline: str) -> str:
        context = json.dumps(context_token)
        base = json.dumps(baseline)
        return (
            "(function(){"
            f"var context={context},base={base};"
            "var root=document.querySelector('[data-wireagent-context=\"'+context+'\"]');"
            "if(!root||!root.isConnected)return 'stale';var imgs=root.querySelectorAll('img');"
            "for(var i=0;i<imgs.length;i++)imgs[i].setAttribute('data-wireagent-media-baseline',base);"
            "return 'baselined';})()"
        )

    @staticmethod
    def _bind_new_media_js(context_token: str, baseline: str, media_token: str) -> str:
        context = json.dumps(context_token)
        base = json.dumps(baseline)
        media = json.dumps(media_token)
        return (
            "(function(){"
            f"var context={context},base={base},media={media};"
            "var root=document.querySelector('[data-wireagent-context=\"'+context+'\"]');"
            "if(!root||!root.isConnected)return 'stale';var imgs=root.querySelectorAll('img');var found=[];"
            "for(var i=0;i<imgs.length;i++){var img=imgs[i];"
            "if(img.getAttribute('data-wireagent-media-baseline')===base)continue;"
            "if(img.getAttribute('data-wireagent-approved-media'))continue;"
            "var tray=img.closest(\"[data-testid='attachments']\");"
            "if(tray||(img.src&&img.src.indexOf('blob:')===0))found.push(img);}"
            "if(found.length!==1)return found.length?'ambiguous':'missing';"
            "found[0].setAttribute('data-wireagent-approved-media',media);return 'bound';})()"
        )

    async def attach_media(self, image_path: str) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        context = self._require_context()
        if context is None:
            return soft_failure("no approved composer context", failure_category=FailureCategory.SECURITY)
        context_token, _, _ = context
        input_token = secrets.token_hex(16)
        baseline = secrets.token_hex(16)
        cdp = self._sb._controller._cdp
        marked = await cdp.evaluate(self._mark_media_input_js(context_token, input_token))
        marked_value = marked.data.get("result", {}).get("value") if marked.ok and marked.data else None
        if marked_value != "bound":
            return soft_failure(
                f"approved composer media input binding failed: {marked_value!r}",
                failure_category=FailureCategory.SECURITY,
            )
        baseline_result = await cdp.evaluate(self._baseline_context_images_js(context_token, baseline))
        baselined = self._require_baselined(baseline_result, "existing composer media")
        if not baselined.ok:
            return baselined
        upload = await self._sb.upload_file(f"[data-wireagent-media-input='{input_token}']", image_path)
        if not upload.ok:
            return upload
        media_token = secrets.token_hex(16)
        for _ in range(12):
            bound = await cdp.evaluate(self._bind_new_media_js(context_token, baseline, media_token))
            value = bound.data.get("result", {}).get("value") if bound.ok and bound.data else None
            if value == "bound":
                return ok_result(data={"attached": True, "provenance_bound": True})
            if value == "ambiguous":
                return soft_failure("multiple new upload previews appeared", failure_category=FailureCategory.SECURITY)
            await asyncio.sleep(0.25)
        return soft_failure(
            "approved upload preview did not appear",
            failure_category=FailureCategory.SELECTOR_NOT_FOUND,
        )

    @staticmethod
    def _content_proof_js(
        context_token: str,
        kind: str,
        target_id: str,
        expected_text: str,
        expected_attachments: int,
        *,
        click: bool,
        submit_token: str,
    ) -> str:
        context = json.dumps(context_token)
        kind_js = json.dumps(kind)
        target = json.dumps(target_id)
        text = json.dumps(expected_text)
        submit = json.dumps(submit_token)
        if click:
            action = (
                "if(btn.getAttribute('data-wireagent-submit-button')!==submit)return 'submit_changed';"
                "btn.removeAttribute('data-wireagent-submit-button');btn.click();return 'clicked';"
            )
        else:
            action = "btn.setAttribute('data-wireagent-submit-button',submit);return 'bound';"
        return (
            "(function(){"
            + M5ScopedWriteBroker._visible_js()
            + f"var context={context},kind={kind_js},target={target},expected={text},"
            f"expectedCount={expected_attachments},submit={submit};"
            "var root=document.querySelector('[data-wireagent-context=\"'+context+'\"]');"
            "if(!vis(root))return 'stale_context';"
            "if(root.getAttribute('data-wireagent-context-kind')!==kind||"
            "root.getAttribute('data-wireagent-context-target')!==target)return 'context_changed';"
            "var ta=root.querySelector(\"[data-testid='tweetTextarea_0']\");"
            "if(!vis(ta)||(ta.innerText||'')!==expected)return 'payload_changed';"
            "var imgs=root.querySelectorAll('img');var candidates=[];"
            "for(var i=0;i<imgs.length;i++){var img=imgs[i];"
            "if(img.getAttribute('data-wireagent-context-preexisting')===context)continue;"
            "var tray=img.closest(\"[data-testid='attachments']\");"
            "if(tray||(img.src&&img.src.indexOf('blob:')===0))candidates.push(img);}"
            "if(candidates.length!==expectedCount)return 'attachments_changed';"
            "for(var j=0;j<candidates.length;j++){"
            "if(!candidates[j].getAttribute('data-wireagent-approved-media'))return 'media_unapproved';"
            "if(!candidates[j].complete||candidates[j].naturalWidth<=0)return 'media_not_ready';}"
            "var btns=root.querySelectorAll(\"[data-testid='tweetButton']\");"
            "if(btns.length!==1||!vis(btns[0]))return 'submit_missing';var btn=btns[0];"
            "if(btn.getAttribute('disabled'))return 'submit_disabled';"
            + action
            + "})()"
        )

    async def click_submit(
        self,
        *,
        _commit_gate: Optional[CommitGate] = None,
        _precommit_check: Optional[PrecommitCheck] = None,
        _expected_text: Optional[str] = None,
        _expected_attachments: Optional[int] = None,
    ) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        if _precommit_check is None or _expected_text is None or _expected_attachments is None:
            return soft_failure("M5 submit requires scoped payload proof", failure_category=FailureCategory.SECURITY)
        context = self._require_context()
        if context is None:
            return soft_failure("approved composer context is missing", failure_category=FailureCategory.SECURITY)
        context_token, kind, target_id = context
        checked = await _precommit_check()
        if checked is not None:
            return checked
        submit_token = secrets.token_hex(16)
        cdp = self._sb._controller._cdp
        bound = await cdp.evaluate(
            self._content_proof_js(
                context_token,
                kind,
                target_id,
                _expected_text,
                _expected_attachments,
                click=False,
                submit_token=submit_token,
            )
        )
        value = bound.data.get("result", {}).get("value") if bound.ok and bound.data else None
        if value != "bound":
            return soft_failure(
                f"scoped composer proof failed: {value!r}",
                failure_category=FailureCategory.SECURITY,
            )
        if (r := self._guard()) is not None:
            return r
        if (denied := self._cross_commit_gate(_commit_gate)) is not None:
            return denied
        clicked = await cdp.evaluate(
            self._content_proof_js(
                context_token,
                kind,
                target_id,
                _expected_text,
                _expected_attachments,
                click=True,
                submit_token=submit_token,
            )
        )
        click_value = clicked.data.get("result", {}).get("value") if clicked.ok and clicked.data else None
        if click_value != "clicked":
            return soft_failure(
                f"scoped composer changed after authority crossing: {click_value!r}",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(data={"submitted": True})

    async def close_composer(self) -> ActionResult:
        try:
            return await super().close_composer()
        finally:
            self._m5_context_token = None
            self._m5_context_kind = None
            self._m5_context_target = None
