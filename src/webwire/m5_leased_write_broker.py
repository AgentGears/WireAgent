"""Browser-session lease for M5 scoped write operations.

``M5ScopedWriteBroker`` proves DOM provenance inside one operation. This wrapper
adds the missing same-process browser-session coordination needed when multiple
M5 write attempts share one ``SuperBrowser`` instance:

- one async write operation at a time per underlying browser;
- one active content-composer owner at a time per browser;
- target-click -> transient-context binding cannot interleave with another M5
  writer;
- focus -> keyboard typing and upload -> preview binding stay inside the lease;
- one-step engagement/delete cannot navigate over an owned content context.

This is still an engineering boundary, not a hostile-code sandbox. Layer 5 must
ensure legacy/read paths do not concurrently navigate the same browser while an
M5 scoped write owns it.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
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
            result = await operation()
            if not result.ok or self._m5_context_token is None:
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
    def _mark_media_input_js(context_token: str, input_token: str) -> str:
        """Bind only an input causally inside the approved composer/form."""
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

    async def fill_composer(self, text: str) -> ActionResult:
        return await self._start_content(lambda: super(M5LeasedWriteBroker, self).fill_composer(text))

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

    async def delete_post(
        self,
        post_url: str,
        post_id: str,
        *,
        _commit_gate: Optional[CommitGate] = None,
    ) -> ActionResult:
        return await self._one_shot(
            lambda: super(M5LeasedWriteBroker, self).delete_post(
                post_url, post_id, _commit_gate=_commit_gate
            )
        )
