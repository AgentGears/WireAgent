"""Read-only browser surface coordinated with the M5 per-browser write lease.

A content write can retain DOM provenance across several awaited operations even
when the short operation mutex is not held. Ordinary read/navigation capability
calls must therefore share the same per-browser lease state: they serialize with
individual M5 write operations and fail closed while any content composer owner
is active.

This class intentionally preserves ``ReadOnlyBroker``'s surface exactly. It adds
coordination only; no mutation authority is introduced.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.broker import ReadOnlyBroker
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, soft_failure
from webwire.m5_leased_write_broker import _browser_state
from webwire.safety.kill_switch import KillSwitch

__all__ = ["M5LeasedReadBroker"]


class M5LeasedReadBroker(ReadOnlyBroker):
    """ReadOnlyBroker whose browser operations share the M5 write lease."""

    def __init__(
        self,
        sb: Any,
        kill_switch: KillSwitch,
        config: Optional[WebWireConfig] = None,
    ) -> None:
        # Live construction passes WireAgent's attribute-capable facade proxy,
        # while isolated tests may pass a direct fake facade. Both expose the
        # same ReadOnlyBroker contract at runtime.
        super().__init__(sb, kill_switch, config)
        self._m5_write_state = _browser_state(sb)

    @staticmethod
    def _composer_busy() -> ActionResult:
        return soft_failure(
            "browser read is blocked by an active M5 content composer",
            failure_category=FailureCategory.SECURITY,
        )

    async def _coordinated(
        self,
        operation: Callable[[], Awaitable[ActionResult]],
    ) -> ActionResult:
        async with self._m5_write_state.lock:
            if self._m5_write_state.content_owner is not None:
                return self._composer_busy()
            return await operation()

    async def navigate(
        self,
        url: str,
        *,
        wait_until: str = "domcontentloaded",
    ) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).navigate(
                url,
                wait_until=wait_until,
            )
        )

    async def reload(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).reload(wait_until=wait_until)
        )

    async def go_back(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).go_back(wait_until=wait_until)
        )

    async def go_forward(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).go_forward(wait_until=wait_until)
        )

    async def observe(self) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).observe()
        )

    async def extract(
        self,
        query: str,
        *,
        selector: Optional[str] = None,
        schema: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).extract(
                query,
                selector=selector,
                schema=schema,
            )
        )

    async def list_tabs(self) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).list_tabs()
        )

    async def switch_tab(self, tab_id: int) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).switch_tab(tab_id)
        )

    async def query_attr(self, selector: str, attr: str) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).query_attr(selector, attr)
        )

    async def query_text(self, selector: str) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).query_text(selector)
        )

    async def enumerate_posts(self) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).enumerate_posts()
        )

    async def probe_selectors(self, specs: dict[str, list[str]]) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).probe_selectors(specs)
        )

    async def scroll(self, pixels: int = 3000) -> ActionResult:
        return await self._coordinated(
            lambda: super(M5LeasedReadBroker, self).scroll(pixels)
        )
