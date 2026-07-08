"""Dispatcher — the single entry point for capability execution.

Responsibilities (Phase 0a):
- Owns the SessionManager (the only holder of the raw SuperBrowser facade).
- Builds the ReadOnlyBroker after session start and hands ONLY the broker to
  capabilities (Point 2 decision B).
- Checks the kill switch at the TOP of every capability run (Point 3, first
  check site; the broker is the second site).
- Journals every capability invocation as one NDJSON record (Point 4).
- Routes unsupported-capability requests through the envelope before touching
  the browser (Point 1).

This is the only place that ties session + kill switch + journal + broker +
registry together. Capabilities never see this object — only the broker.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from super_browser.results.types import SuccessCategory

from webwire.broker import ReadOnlyBroker
from webwire.capabilities.base import Capability, CapabilityTier
from webwire.capabilities.health import HealthCapability
from webwire.capabilities.read import ReadCapability
from webwire.capabilities.whoami import WhoamiCapability
from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, unsupported_capability
from webwire.journal import BrowserActionEntry, Journal, JournalRecord
from webwire.registry import CapabilityRegistry
from webwire.safety import KillSwitch
from webwire.session import SessionManager

logger = logging.getLogger(__name__)

__all__ = ["Dispatcher"]


class Dispatcher:
    """Single entry point: ``await dispatcher.invoke(name, input)``."""

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        session_manager: Optional[SessionManager] = None,
    ) -> None:
        self._config = config or WebWireConfig()
        self._session = session_manager or SessionManager(self._config)
        self._kill = KillSwitch(self._config)
        self._journal = Journal(self._config)
        self._registry = CapabilityRegistry()
        self._broker: Optional[ReadOnlyBroker] = None
        self._registered_default = False
        # Register default capabilities eagerly so the registry is introspectable
        # and the unsupported-capability path works even before session start.
        # (whoami needs no deps; health needs the kill switch + config, both
        # already constructed above.)
        self._register_defaults()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> ActionResult:
        """Start the session, restore persisted cookies, register capabilities."""
        r = await self._session.start()
        if not r.ok:
            return r
        sb = self._session.sb
        if sb is None:
            from webwire.envelope import hard_failure
            from super_browser.results.types import FailureCategory
            return hard_failure(
                "Session reported started but sb is None",
                failure_category=FailureCategory.BROWSER_CRASH,
            )
        # Restore persisted cookies BEFORE first navigation (review Q3).
        # Non-fatal: whoami is the real auth gate.
        if self._session.session_loaded_state == "pending":
            try:
                await self._session.restore_session_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning("session restore failed: %r", exc)
        self._broker = ReadOnlyBroker(sb, self._kill, self._config)
        # Defaults already registered in __init__; ensure idempotent.
        self._register_defaults()
        # Enrich the start result with session/ownership state for callers.
        try:
            data = r.data or {}
            data["ownership"] = self._session.ownership
            data["session"] = self._session.session_loaded_state
        except Exception:  # noqa: BLE001
            pass
        return r

    async def stop(self) -> ActionResult:
        """Stop the session. Does not trip the kill switch."""
        return await self._session.stop()

    # -- invocation ----------------------------------------------------------

    async def invoke(self, name: str, input: Optional[dict[str, Any]] = None) -> ActionResult:
        """Invoke a capability by name. The main entry point.

        Order of guards:
        1. Kill switch (top of dispatcher — Point 3 first site).
        2. Capability exists (unsupported returned before touching browser).
        3. Broker available (session started).
        Then run, journal, return.
        """
        input = input or {}
        started_monotonic = time.monotonic()
        trace_id = _new_trace_id()
        capability = self._registry.get(name)

        # Kill switch FIRST (Point 3 first site). Review-iteration adjustment:
        # kill must dominate every other resolution so the operator invariant
        # is absolute — "when killed, every invocation returns killed." This
        # includes unsupported capability names (safe: they never reach the
        # browser, but the cleaner mental model is global-shut-on-trip).
        if self._kill.tripped():
            from webwire.envelope import kill_switched
            result = kill_switched()
            self._journal_write(
                trace_id=trace_id, capability=name, input=input,
                result=result, policy_decision="killed",
                actions=[], started_monotonic=started_monotonic,
            )
            return result

        # Unsupported capability — before touching the browser.
        if capability is None:
            result = unsupported_capability(name)
            self._journal_write(
                trace_id=trace_id, capability=name, input=input,
                result=result, policy_decision="unsupported",
                actions=[], started_monotonic=started_monotonic,
            )
            return result

        if self._broker is None:
            from webwire.envelope import hard_failure
            from super_browser.results.types import FailureCategory
            result = hard_failure(
                "Dispatcher not started — call await dispatcher.start() first",
                failure_category=FailureCategory.BROWSER_CRASH,
            )
            self._journal_write(
                trace_id=trace_id, capability=name, input=input,
                result=result, policy_decision="denied",
                actions=[], started_monotonic=started_monotonic,
            )
            return result

        # Run the capability. The broker (with its own kill-switch guard) is
        # the only browser surface the capability sees.
        try:
            result = await capability.run(self._broker, input)
        except Exception as exc:  # noqa: BLE001 — envelope the error
            logger.exception("Capability %r raised", name)
            from webwire.envelope import hard_failure
            from super_browser.results.types import FailureCategory
            result = hard_failure(
                f"Capability {name!r} raised: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

        # Session checkpoint policy (review Q3): on a successful whoami, mark
        # authenticated and eagerly checkpoint the cookie jar. whoami is the
        # only capability that establishes identity; other capabilities benefit
        # from the checkpoint but don't trigger it. This keeps capabilities pure
        # (no persistence coupling) — policy lives in the dispatcher.
        if name == "whoami" and result.ok:
            self._session.mark_authenticated()
            try:
                await self._session.checkpoint_session()
            except Exception as exc:  # noqa: BLE001
                logger.warning("post-whoami checkpoint failed: %r", exc)

        self._journal_write(
            trace_id=trace_id, capability=name, input=input,
            result=result, policy_decision="allowed",
            actions=[], started_monotonic=started_monotonic,
        )
        return result

    # -- accessors -----------------------------------------------------------

    @property
    def capabilities(self) -> list[str]:
        return self._registry.names()

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill

    @property
    def session_manager(self) -> SessionManager:
        return self._session

    # -- internals -----------------------------------------------------------

    def _register_defaults(self) -> None:
        if self._registered_default:
            return
        # whoami — pure read, no extra deps.
        self._registry.register(WhoamiCapability())
        # health — needs kill switch + session manager + config for diagnostics.
        self._registry.register(HealthCapability(self._kill, self._session, self._config))
        # read — the golden read (Phase 1).
        self._registry.register(ReadCapability())
        self._registered_default = True

    def _journal_write(
        self,
        *,
        trace_id: str,
        capability: str,
        input: dict[str, Any],
        result: ActionResult,
        policy_decision: str,
        actions: list[BrowserActionEntry],
        started_monotonic: float,
    ) -> None:
        duration_ms = (time.monotonic() - started_monotonic) * 1000
        err = result.error
        record = JournalRecord(
            timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            trace_id=trace_id,
            capability=capability,
            target=_redact_target(input),
            input_redacted=_redact_input(input),
            kill_switch_tripped=self._kill.tripped(),
            policy_decision=policy_decision,
            result_ok=bool(result.ok),
            success_category=(result.success_category.value if result.success_category else None),
            failure_category=(result.failure_category.value if result.failure_category else None),
            error_message=(err.message if err else None),
            browser_actions=[a.__dict__ for a in actions] if actions else [],
            duration_ms=duration_ms,
        )
        # Screenshot policy: default failure-only (Point 4 decision).
        if self._journal.should_capture_screenshot(failed=not result.ok):
            # Phase 0a: screenshot capture is plumbed but deferred — capturing
            # requires a broker method we deliberately didn't expose (no raw
            # page access). Record intent only; actual capture lands with a
            # dedicated broker.diagnostic_screenshot() in a follow-up.
            record.screenshot = None  # captured=False, intentionally
        self._journal.append(record)


def _new_trace_id() -> str:
    import uuid
    return str(uuid.uuid4())


def _redact_target(input: dict[str, Any]) -> Optional[str]:
    """Best-effort target redaction — keep origin+path, drop query/fragment.

    Picks the first URL-like value in the input, regardless of key name, since
    capabilities use varied keys (url, post_url, target, home_url, ...).
    """
    for val in input.values():
        if isinstance(val, str) and "://" in val:
            scheme, rest = val.split("://", 1)
            path = rest.split("?", 1)[0].split("#", 1)[0]
            return f"{scheme}://{path}"
    return None


def _redact_input(input: dict[str, Any]) -> dict[str, Any]:
    """Return a shallow copy with URL-like values redacted to origin+path."""
    out: dict[str, Any] = {}
    for k, v in input.items():
        if isinstance(v, str) and "://" in v:
            scheme, rest = v.split("://", 1)
            path = rest.split("?", 1)[0].split("#", 1)[0]
            out[k] = f"{scheme}://{path}"
        else:
            out[k] = v
    return out
