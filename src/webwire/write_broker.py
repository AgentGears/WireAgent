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

    # ------------------------------------------------------------------
    # Post port (PostWritePort) — Phase 4b
    # ------------------------------------------------------------------

    async def fill_composer(self, text: str) -> ActionResult:
        """Navigate to the compose page and fill the text composer with exactly
        the provided text. Does NOT submit.

        X's composer: navigate to x.com/compose/post, then fill the
        data-testid='tweetTextarea_0' contenteditable div.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate("https://x.com/compose/post", wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)  # hydrate the compose page (DraftJS needs time)
        try:
            # Click the textarea to focus it, then type the text.
            click_r = await self._sb.click(
                "[data-testid='tweetTextarea_0']",
                description="post composer textarea",
            )
            if not click_r.ok:
                return soft_failure(
                    "Could not focus composer textarea.",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            await asyncio.sleep(1)
            # X's composer is a DraftJS contenteditable — fill() and
            # type_text(selector, text) don't work because Draft manages its
            # own state. Use backend_page.keyboard.type() which sends real
            # keyboard events to the focused element — Draft picks these up.
            page = self._sb._page  # type: ignore[attr-defined]
            backend_page = page.engine_page.backend_page  # type: ignore[attr-defined]
            await backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(1)
            return ok_result(data={"filled": True, "text_length": len(text)})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_composer error: {exc!r}")

    async def read_composer_text(self) -> ActionResult:
        """Read the current text in the composer DOM. Used for the pre-submit
        assertion (ChatGPT's step 10: composer_dom_text == normalized_text)."""
        if (r := self._guard()) is not None:
            return r
        cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
        # NOTE: the selector contains single quotes (data-testid='tweetTextarea_0')
        # which conflict with JS string delimiters. Use double quotes in the JS
        # and escape properly. This was a real bug — the unescaped single quote
        # in the selector silently broke querySelector, returning empty text.
        expr = (
            '(function(){var el=document.querySelector("[data-testid=\'tweetTextarea_0\']");'
            'return el?el.innerText:"";})()'
        )
        try:
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                text = result.data.get("result", {}).get("value") or ""
                return ok_result(data={"composer_text": text})
            return ok_result(data={"composer_text": ""})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"read_composer_text error: {exc!r}")

    async def click_submit(self) -> ActionResult:
        """Click the post submit button. Semantic, not generic — targets
        data-testid='tweetButton' specifically."""
        if (r := self._guard()) is not None:
            return r
        try:
            return await self._sb.click(
                "[data-testid='tweetButton']",
                description="post submit button",
            )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"click_submit error: {exc!r}")

    async def capture_posted_url(self) -> ActionResult:
        """After clicking submit, capture the posted post's URL.

        X navigates to the new post's page after posting (or shows it in the
        timeline). We wait briefly, then check the URL + look for the most
        recent article with a /status/ href.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        await asyncio.sleep(4)  # wait for navigation to the new post

        # Check if the URL is now at a /status/ page.
        obs = await self._sb.observe()
        url = ""
        if obs.ok and obs.data:
            url = obs.data.get("url", "") or ""

        # If we're on a /status/ page, that's the posted URL.
        import re
        m = re.search(r"/status/(\d+)", url)
        if m:
            return ok_result(data={
                "posted_url": url,
                "posted_post_id": m.group(1),
            })

        # Fallback: look for the first article with a /status/ href (the new post).
        cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            "(function(){"
            "var a=document.querySelector(\"a[href*='/status/']\");"
            "return a?a.getAttribute('href'):null;"
            "})()"
        )
        try:
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                href = result.data.get("result", {}).get("value")
                if href:
                    m2 = re.search(r"/status/(\d+)", href)
                    if m2:
                        full_url = f"https://x.com{href}" if href.startswith("/") else href
                        return ok_result(data={
                            "posted_url": full_url,
                            "posted_post_id": m2.group(1),
                        })
        except Exception:  # noqa: BLE001
            pass

        # Could not capture — this is a degraded result (submit may have worked).
        return ok_result(data={
            "posted_url": None,
            "posted_post_id": None,
            "note": "submit_clicked_verification_pending",
        })

    # ------------------------------------------------------------------
    # Reply port (Phase 4c) — target-scoped reply
    # ------------------------------------------------------------------

    async def open_reply_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        """Navigate to the target post, find the article matching target_post_id,
        click the reply button inside THAT article (not the first visible one),
        then type text via keyboard.

        ChatGPT's invariant #4: click must be scoped to the target article.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)

        try:
            # Find the article with the matching post_id and click its reply button.
            # X's reply button: data-testid='reply' inside the article.
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            # Use CDP to find + click the reply button on the target article.
            # First, locate the article by its status href.
            expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                f'for(var i=0;i<links.length;i++){{'
                f'var href=links[i].getAttribute("href");'
                f'if(href&&href.indexOf("/status/{target_post_id}")>=0){{'
                f'var art=links[i].closest("article");'
                f'if(art){{var btn=art.querySelector("[data-testid=\'reply\']");'
                f'if(btn){{btn.click();return "clicked";}}'
                f'return "no_reply_button";}}}}}}'
                f'return "target_not_found";'
                f'}})()'
            )
            result = await cdp.evaluate(expr)
            click_result = result.data.get("result", {}).get("value") if (result.ok and result.data) else None

            if click_result != "clicked":
                return soft_failure(
                    f"open_reply_on_target: {click_result} for post {target_post_id}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            await asyncio.sleep(2)  # wait for reply composer to open
            return ok_result(data={"reply_opened": True, "target_post_id": target_post_id})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"open_reply_on_target error: {exc!r}")

    async def fill_reply_composer(self, text: str) -> ActionResult:
        """Type text into the reply composer (already opened by open_reply_on_target).
        Uses keyboard.type for DraftJS compatibility."""
        if (r := self._guard()) is not None:
            return r
        import asyncio
        try:
            # The reply composer is a DraftJS contenteditable, same as the post composer.
            # Click it to ensure focus, then type via keyboard.
            await self._sb.click(
                "[data-testid='tweetTextarea_0']",
                description="reply composer textarea",
            )
            await asyncio.sleep(0.5)
            page = self._sb._page  # type: ignore[attr-defined]
            backend_page = page.engine_page.backend_page  # type: ignore[attr-defined]
            await backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(1)
            return ok_result(data={"filled": True, "text_length": len(text)})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_reply_composer error: {exc!r}")

    # ------------------------------------------------------------------
    # Quote port (Phase 4d) — target-scoped quote
    # ------------------------------------------------------------------

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> ActionResult:
        """Navigate to the target post, find the article matching target_post_id,
        click the repost button inside THAT article, then click 'Quote' from
        the popup menu. This opens the quote composer with the target embedded.

        ChatGPT's invariant #4: quote action must be target-scoped.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        nav = await self._sb.navigate(post_url, wait_until="domcontentloaded")
        if not nav.ok:
            return nav
        await asyncio.sleep(4)

        try:
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            # Step 1: find the target article and click its repost button.
            click_repost_expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                f'for(var i=0;i<links.length;i++){{'
                f'var href=links[i].getAttribute("href");'
                f'if(href&&href.indexOf("/status/{target_post_id}")>=0){{'
                f'var art=links[i].closest("article");'
                f'if(art){{var btn=art.querySelector("[data-testid=\'retweet\']");'
                f'if(btn){{btn.click();return "clicked";}}'
                f'return "no_repost_button";}}}}}}'
                f'return "target_not_found";'
                f'}})()'
            )
            result = await cdp.evaluate(click_repost_expr)
            repost_result = result.data.get("result", {}).get("value") if (result.ok and result.data) else None

            if repost_result != "clicked":
                return soft_failure(
                    f"open_quote_on_target: could not click repost on {target_post_id}: {repost_result}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            await asyncio.sleep(1.5)  # wait for the repost menu to appear

            # Step 2: click "Quote" from the popup menu.
            click_quote_expr = (
                '(function(){'
                'var items=document.querySelectorAll("[role=\'menuitem\']");'
                'for(var i=0;i<items.length;i++){'
                'var text=items[i].innerText||"";'
                'if(text.trim()==="Quote"){items[i].click();return "clicked";}'
                '}'
                'return "no_quote_menuitem";'
                '})()'
            )
            result2 = await cdp.evaluate(click_quote_expr)
            quote_result = result2.data.get("result", {}).get("value") if (result2.ok and result2.data) else None

            if quote_result != "clicked":
                return soft_failure(
                    f"open_quote_on_target: could not click Quote menu item: {quote_result}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            await asyncio.sleep(2)  # wait for quote composer to open
            return ok_result(data={"quote_opened": True, "target_post_id": target_post_id})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"open_quote_on_target error: {exc!r}")

    async def fill_quote_composer(self, text: str) -> ActionResult:
        """Type text into the quote composer (already opened by open_quote_on_target).
        Uses keyboard.type for DraftJS compatibility."""
        if (r := self._guard()) is not None:
            return r
        import asyncio
        try:
            await self._sb.click(
                "[data-testid='tweetTextarea_0']",
                description="quote composer textarea",
            )
            await asyncio.sleep(0.5)
            page = self._sb._page  # type: ignore[attr-defined]
            backend_page = page.engine_page.backend_page  # type: ignore[attr-defined]
            await backend_page.keyboard.type(text, delay=10)
            await asyncio.sleep(1)
            return ok_result(data={"filled": True, "text_length": len(text)})
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"fill_quote_composer error: {exc!r}")

    # ------------------------------------------------------------------
    # Media port (v0.2 M1) — photo upload
    # ------------------------------------------------------------------

    async def attach_media(self, image_path: str) -> ActionResult:
        """Upload an image file to the X compose page via the file input.

        Must be called AFTER fill_composer (or fill_reply/quote_composer) —
        the composer must be open. X's compose page has an
        input[type='file'] for image upload.

        ChatGPT concern #6: upload is a state machine. After upload_file(),
        wait for the attachment preview to appear before returning.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        try:
            # X's image upload input: a hidden input[type='file'] on the compose page.
            upload_r = await self._sb.upload_file(
                "input[type='file']",
                image_path,
            )
            if not upload_r.ok:
                return soft_failure(
                    f"attach_media: upload_file failed: {upload_r.error.message if upload_r.error else upload_r}",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            # State machine: wait for the attachment preview to appear.
            # X shows [data-testid='attachments'] or image previews after upload.
            await asyncio.sleep(2)  # initial processing
            # Verify attachment preview is present.
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            check_expr = (
                '(function(){'
                'var att=document.querySelector("[data-testid=\'attachments\']");'
                'if(att)return "attachment_preview";'
                'var imgs=document.querySelectorAll("img");'
                'for(var i=0;i<imgs.length;i++){'
                'if(imgs[i].src&&imgs[i].src.indexOf("blob:")>=0)return "blob_image";'
                'if(imgs[i].src&&imgs[i].naturalWidth>100)return "large_image";'
                '}'
                'return "no_preview";'
                '})()'
            )
            # Retry a few times for the preview to appear.
            for attempt in range(5):
                result = await cdp.evaluate(check_expr)
                state = result.data.get("result", {}).get("value") if (result.ok and result.data) else None
                if state and state != "no_preview":
                    return ok_result(data={"attached": True, "preview_state": state})
                await asyncio.sleep(1)

            return soft_failure(
                "attach_media: attachment preview did not appear after upload (timeout).",
                failure_category=FailureCategory.TIMEOUT if hasattr(FailureCategory, "TIMEOUT") else FailureCategory.UNKNOWN,
            )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"attach_media error: {exc!r}")

    async def verify_attachment_ready(self) -> ActionResult:
        """Verify X has finished processing the attachment (ChatGPT concern #6).

        Checks: no processing spinner, attachment preview present, submit
        button is enabled. Returns ok=True when ready.
        """
        if (r := self._guard()) is not None:
            return r
        import asyncio
        try:
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            # Check that the submit button is enabled (not disabled while processing).
            expr = (
                '(function(){'
                'var btn=document.querySelector("[data-testid=\'tweetButton\']");'
                'if(!btn)return "no_submit_button";'
                'if(btn.getAttribute("disabled"))return "disabled";'
                'return "enabled";'
                '})()'
            )
            for attempt in range(5):
                result = await cdp.evaluate(expr)
                state = result.data.get("result", {}).get("value") if (result.ok and result.data) else None
                if state == "enabled":
                    return ok_result(data={"attachment_ready": True})
                await asyncio.sleep(1)
            return soft_failure(
                f"verify_attachment_ready: submit button {state} after 5s.",
                failure_category=FailureCategory.TIMEOUT if hasattr(FailureCategory, "TIMEOUT") else FailureCategory.UNKNOWN,
            )
        except Exception as exc:  # noqa: BLE001
            return soft_failure(f"verify_attachment_ready error: {exc!r}")
