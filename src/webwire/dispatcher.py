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
from webwire.capabilities.read_profile import ReadProfileCapability
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
        # Write-safety kernel (Phase 0b). WRITE-tier capabilities route through
        # this instead of calling .run() directly.
        from webwire.safety import (
            DEFAULT_REGISTRY,
            DedupeStore,
            TokenBucket,
            WriteKernel,
        )
        self._dedupe = DedupeStore(ttl_seconds=3600)
        # WriteBroker factory: constructs a narrow WriteBroker from the live
        # SuperBrowser session. Called by the kernel during execute/verify.
        # Defined as a closure so it captures self._session (which isn't
        # populated until start()).
        def _make_write_broker():
            from webwire.write_broker import WriteBroker
            sb = self._session.sb
            if sb is None:
                raise RuntimeError("Cannot create WriteBroker: session not started")
            return WriteBroker(sb, self._kill)
        self._write_kernel = WriteKernel(
            kill_switch=self._kill,
            risk_registry=DEFAULT_REGISTRY,
            token_bucket=TokenBucket(),
            dedupe=self._dedupe,
            journal=self._journal,
            write_broker_factory=_make_write_broker,
        )
        # Register default capabilities eagerly so the registry is introspectable
        # and the unsupported-capability path works even before session start.
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
        # Hydrate the dedupe store from recent journal entries (Phase 0b review
        # requirement): closes the restart-during-loop hole. Safe no-op if the
        # journal is empty or doesn't exist yet.
        try:
            hydrated = self._dedupe.hydrate_from_journal(self._config.journal_path())
            if hydrated:
                logger.info("DedupeStore hydrated %d entries from journal", hydrated)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dedupe hydration failed: %r", exc)
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
        # Phase 0b: WRITE-tier capabilities route through the WriteKernel pipeline
        # (compose→preview→policy→confirm→execute→journal→verify). READ capabilities
        # call .run() directly.
        try:
            if capability.tier == CapabilityTier.WRITE:
                # Actor identity: resolved per-invocation from the session's
                # authenticated handle (set by whoami). None until whoami runs.
                actor = getattr(self._session, '_resolved_handle', None)
                result = await self._write_kernel.execute(
                    capability, self._broker, input, actor_identity=actor,
                )
            else:
                # download_image uses the DownloadBroker (separate local-output
                # boundary), not the ReadOnlyBroker.
                if name == "download_image":
                    from webwire.download_broker import DownloadBroker
                    dl_dir = self._config.state_dir / "downloads"
                    dl_broker = DownloadBroker(
                        self._session.sb, self._kill, dl_dir,
                    )
                    result = await capability.run(dl_broker, input)
                else:
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
        # read_profile — fan-out: enumerate a profile's posts (Phase 2).
        self._registry.register(ReadProfileCapability())
        # read_thread — visible conversation-slice reader (Phase 2b).
        from webwire.capabilities.read_thread import ReadThreadCapability
        self._registry.register(ReadThreadCapability())
        # read_search — search X for posts (Phase 2c).
        from webwire.capabilities.read_search import ReadSearchCapability
        self._registry.register(ReadSearchCapability())
        # bookmark_post — first write capability (Phase 3 canary). Routes
        # through the WriteKernel pipeline.
        from webwire.capabilities.bookmark import BookmarkCapability
        self._registry.register(BookmarkCapability())
        # like_post — public engagement canary (Phase 3b). Pre-existing-state
        # aware: already-liked → already_satisfied no-op, no compensation.
        from webwire.capabilities.like import LikeCapability
        self._registry.register(LikeCapability())
        # compose_post — Phase 4a dry-run compose (NO submit). Proves the
        # irreversible-write safety contract before any public post is possible.
        from webwire.capabilities.compose_post import ComposePostCapability
        self._registry.register(ComposePostCapability())
        # post_text — Phase 4b first live public content write. The actual
        # submit path for posting text to X. Routes through the full safety
        # pipeline with composer read-back assertion + final kill check.
        from webwire.capabilities.post_text import PostTextCapability
        self._registry.register(PostTextCapability())
        # reply_post — Phase 4c target-bound reply. Reuses post_text machinery
        # with target_post_id binding + target-scoped reply button click.
        from webwire.capabilities.reply_post import ReplyPostCapability
        self._registry.register(ReplyPostCapability())
        # quote_post — Phase 4d target-bound quote (the final capability).
        # Target-scoped repost→Quote flow + dual verification (text + quote attachment).
        from webwire.capabilities.quote_post import QuotePostCapability
        self._registry.register(QuotePostCapability())
        # post_photo — v0.2 M1: post text + image. Extends write pipeline
        # with media validation, attachment state machine, attachment verification.
        from webwire.capabilities.post_photo import PostPhotoCapability
        self._registry.register(PostPhotoCapability())
        # download_image — v0.2 M2: download a tweet image to local filesystem.
        # Uses the separate DownloadBroker (local-output boundary), not ReadOnlyBroker.
        from webwire.capabilities.download_image import DownloadImageCapability
        self._registry.register(DownloadImageCapability())
        # reply_photo — v0.2 M3a: reply with text + image. Combines target-scoped
        # reply flow with media pipeline. Composition atomicity enforced.
        from webwire.capabilities.reply_photo import ReplyPhotoCapability
        self._registry.register(ReplyPhotoCapability())
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
