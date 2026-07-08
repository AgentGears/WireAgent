"""Read-only browser-action broker.

Per the Phase 0a design (Point 2 decision B):
- Capability code NEVER receives the raw Super-Browser facade.
- The dispatcher owns the real ``sb`` instance (via SessionManager).
- Capabilities receive only this broker interface.
- The broker exposes only explicitly-allowed read/navigation methods.
- The broker does NOT expose: click, fill, act, delegate, upload_file,
  download, check, uncheck, type_text, or any controller-level mutating
  primitive.
- Every method checks the kill switch at entry (Point 3 — second check site).

Same-surface navigation rule: ``navigate`` only accepts URLs matching the
configured ``allowed_url_prefixes`` (default https://x.com/ , https://twitter.com/).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser import SuperBrowser

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, policy_blocked
from webwire.safety import KillSwitch

logger = logging.getLogger(__name__)

__all__ = ["ReadOnlyBroker"]


class ReadOnlyBroker:
    """The only browser surface capabilities are allowed to touch.

    Constructed by the dispatcher after session start, handed to capabilities.
    Holds a reference to the live ``SuperBrowser`` but exposes only safe
    methods. The raw facade is never leaked.
    """

    # Allowlist of facade methods the broker may delegate to. Anything NOT in
    # this set is unreachable through the broker by construction.
    _ALLOWED = frozenset({
        "navigate", "reload", "go_back", "go_forward",
        "observe", "extract",
        "list_tabs", "switch_tab",
        "query_attr", "query_text",
    })

    def __init__(
        self,
        sb: SuperBrowser,
        kill_switch: KillSwitch,
        config: Optional[WebWireConfig] = None,
    ) -> None:
        self._sb = sb
        self._kill = kill_switch
        self._config = config or WebWireConfig()

    # -- kill-switch guard (second check site, Point 3) ----------------------

    def _guard(self) -> Optional[ActionResult]:
        return self._kill.guard()

    # -- navigation (same-surface constrained) -------------------------------

    async def navigate(self, url: str, *, wait_until: str = "domcontentloaded") -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        if not self._url_allowed(url):
            return policy_blocked(
                f"navigate() target outside allowed surfaces: {self._redact_url(url)}"
            )
        return await self._sb.navigate(url, wait_until=wait_until)

    async def reload(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.reload(wait_until=wait_until)

    async def go_back(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.go_back(wait_until=wait_until)

    async def go_forward(self, *, wait_until: str = "domcontentloaded") -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.go_forward(wait_until=wait_until)

    # -- inspection (pure read) ----------------------------------------------

    async def observe(self) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.observe()

    async def extract(
        self,
        query: str,
        *,
        selector: Optional[str] = None,
        schema: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.extract(query, selector=selector, schema=schema)

    # -- tabs (read + switch only; NO close_tab — that mutates state) --------

    async def list_tabs(self) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.list_tabs()

    async def switch_tab(self, tab_id: int) -> ActionResult:
        if (r := self._guard()) is not None:
            return r
        return await self._sb.switch_tab(tab_id)

    # -- bounded DOM read primitives (read-only, scoped) ---------------------
    # query_attr and query_text are bounded read primitives: the caller supplies
    # a selector (and for query_attr, an attr name), NOT executable JS. The broker
    # owns the fixed expression shape. These stay within the read-only contract
    # (review invariant: "bounded, non-mutating read primitive with no caller-
    # controlled executable code"). They unblock reads that observe() (AX
    # snapshot) and extract() (textContent) cannot serve — hrefs and innerText.

    async def _cdp_read(self, expr: str, label: str) -> ActionResult:
        """Shared CDP-evaluate read helper. Returns ok+value or soft failure."""
        try:
            cdp = self._sb._controller._cdp  # type: ignore[attr-defined]
            result = await cdp.evaluate(expr)
            if result.ok and "exceptionDetails" not in result.data:
                value = result.data.get("result", {}).get("value")
                from webwire.envelope import ok_result
                from super_browser.results.types import SuccessCategory
                return ok_result(data=value, success_category=SuccessCategory.INSPECTION)
            from webwire.envelope import soft_failure
            from super_browser.results.types import FailureCategory
            return soft_failure(
                f"{label}: CDP evaluate failed",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )
        except Exception as exc:  # noqa: BLE001
            from webwire.envelope import hard_failure
            from super_browser.results.types import FailureCategory
            return hard_failure(f"{label} error: {exc!r}")

    async def query_attr(self, selector: str, attr: str) -> ActionResult:
        """Read ONE DOM attribute. Read-only and scoped.

        document.querySelector(sel).getAttribute(attr). Returns value (str|None)
        in data on success.
        """
        if (r := self._guard()) is not None:
            return r
        safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        safe_attr = attr.replace("\\", "\\\\").replace("'", "\\'")
        expr = (
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            f"return el?el.getAttribute('{safe_attr}'):null;}})()"
        )
        r = await self._cdp_read(expr, f"query_attr({selector!r},{attr!r})")
        if r.ok:
            # Wrap into a descriptive data dict for callers that want context.
            r.data = {"selector": selector, "attr": attr, "value": r.data}
        return r

    async def query_text(self, selector: str) -> ActionResult:
        """Read the innerText of ONE element. Read-only and scoped.

        document.querySelector(sel).innerText. Resolves text from nested spans
        (X renders tweetText through nested span/emoji/bidi nodes where direct
        textContent returns None). innerText is layout-aware; acceptable for
        Phase 1 since the contract wants human-visible text, not byte-exact DOM.
        Returns value (str|None) in data on success.
        """
        if (r := self._guard()) is not None:
            return r
        safe_sel = selector.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        expr = (
            f"(function(){{var el=document.querySelector('{safe_sel}');"
            f"return el?el.innerText:null;}})()"
        )
        r = await self._cdp_read(expr, f"query_text({selector!r})")
        if r.ok:
            r.data = {"selector": selector, "value": r.data}
        return r

    # -- diagnostics ---------------------------------------------------------

    @property
    def allowed_methods(self) -> frozenset[str]:
        """The fixed allowlist, for health/journal reporting."""
        return self._ALLOWED

    # -- internals -----------------------------------------------------------

    def _url_allowed(self, url: str) -> bool:
        """Origin-based enforcement, NOT prefix string matching.

        Review-iteration adjustment: the prior ``url.startswith(prefix)`` check
        was defeated by look-alike hosts (``https://x.com.evil.example/``).
        Parse the URL and require scheme=https + host in the exact allowlist.
        Reject deceptive forms: userinfo, non-https schemes, look-alike hosts.
        """
        from urllib.parse import urlsplit

        try:
            parts = urlsplit(url)
        except ValueError:
            return False
        # Scheme must be exactly https.
        if parts.scheme.lower() != "https":
            return False
        # Reject userinfo (``https://user:pass@x.com/``) — deceptive form.
        if parts.username or parts.password:
            return False
        # Host must exactly match an allowed host (no suffix matching).
        host = (parts.hostname or "").lower()
        if host not in self._allowed_hosts:
            return False
        return True

    @property
    def _allowed_hosts(self) -> frozenset[str]:
        """Allowed hosts derived from config at construction, lowercased.

        Computed once from ``allowed_url_prefixes`` so we don't re-parse the
        config on every navigate(). Stored on first access.
        """
        cached = getattr(self, "_allowed_hosts_cached", None)
        if cached is not None:
            return cached
        from urllib.parse import urlsplit

        hosts: set[str] = set()
        for prefix in self._config.allowed_url_prefixes:
            try:
                p = urlsplit(prefix)
                if p.hostname:
                    hosts.add(p.hostname.lower())
            except ValueError:
                continue
        frozen = frozenset(hosts)
        self._allowed_hosts_cached = frozen  # type: ignore[attr-defined]
        return frozen

    @staticmethod
    def _redact_url(url: str) -> str:
        """Minimal URL redaction for logs — keep origin+path, drop query/fragment."""
        if "://" not in url:
            return "<invalid>"
        scheme, rest = url.split("://", 1)
        path = rest.split("?", 1)[0].split("#", 1)[0]
        return f"{scheme}://{path}"
