"""Dispatcher — the single entry point for capability execution.

The dispatcher owns the browser session, read broker, kill switch, invocation
journal, write-policy kernel, and the staged M5 live execution stack. Capability
code never receives the raw SuperBrowser facade.

Layer-5 migration routes all supported live remote mutations through M5 scoped
authority. ``compose_post`` remains a dry-run WRITE shell with no remote effect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Optional

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

if TYPE_CHECKING:
    from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter
    from webwire.safety.m5_delete_adapter import M5DeleteCapabilityAdapter
    from webwire.safety.m5_live_runtime import M5LiveExecutionStack
    from webwire.safety.m5_media_adapter import M5MediaCapabilityAdapter
    from webwire.safety.m5_post_text_adapter import M5PostTextCapabilityAdapter
    from webwire.safety.m5_quote_adapter import M5QuoteCapabilityAdapter
    from webwire.safety.m5_reply_adapter import M5ReplyCapabilityAdapter
    from webwire.safety.write_kernel import WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["Dispatcher"]

_M5_ENGAGEMENT_CAPABILITIES = frozenset({"bookmark_post", "like_post"})
_M5_POST_TEXT_CAPABILITY = "post_text"
_M5_REPLY_CAPABILITY = "reply_post"
_M5_QUOTE_CAPABILITY = "quote_post"
_M5_DELETE_CAPABILITY = "delete_post"
_M5_MEDIA_CAPABILITIES = frozenset(
    {
        "post_photo",
        "reply_photo",
        "quote_photo",
        "post_multi_image",
        "reply_multi_image",
        "quote_multi_image",
    }
)
_M5_MIGRATED_CAPABILITIES = _M5_ENGAGEMENT_CAPABILITIES | _M5_MEDIA_CAPABILITIES | {
    _M5_POST_TEXT_CAPABILITY,
    _M5_REPLY_CAPABILITY,
    _M5_QUOTE_CAPABILITY,
    _M5_DELETE_CAPABILITY,
}


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
        # During staged Layer-5 migration, legacy writes/downloads and M5 reads
        # still share one browser. Serialize supported Dispatcher invocations so
        # no sibling task can navigate over an owned M5 composer. Same-task
        # re-entry is allowed to avoid deadlocking trusted orchestration hooks.
        self._invoke_lock = asyncio.Lock()
        self._invoke_lock_owner: Optional[asyncio.Task[Any]] = None

        from webwire.safety import (
            DEFAULT_EFFECT_POLICIES,
            DEFAULT_REGISTRY,
            DedupeStore,
            EffectLedger,
            TokenBucket,
            WriteKernel,
        )
        from webwire.safety.commit_gateway import CommitGateway
        from webwire.safety.execution_models import AuthorizationEpoch

        self._dedupe = DedupeStore(ttl_seconds=3600)
        self._bucket = TokenBucket()

        # M5 safety authority exists before a browser is live. The gateway owns
        # the exact same KillSwitch as Dispatcher, so a trip revokes outstanding
        # grant/permit epochs even if no capability is currently running.
        self._m5_ledger = EffectLedger(self._config)
        self._m5_epoch = AuthorizationEpoch()
        self._m5_gateway = CommitGateway(
            ledger=self._m5_ledger,
            kill_switch=self._kill,
            authorization_epoch=self._m5_epoch,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        self._m5_stack: Optional[M5LiveExecutionStack] = None
        self._m5_canary_adapters: dict[str, M5EngagementCapabilityAdapter] = {}
        self._m5_post_text_adapter: Optional[M5PostTextCapabilityAdapter] = None
        self._m5_reply_adapter: Optional[M5ReplyCapabilityAdapter] = None
        self._m5_quote_adapter: Optional[M5QuoteCapabilityAdapter] = None
        self._m5_delete_adapter: Optional[M5DeleteCapabilityAdapter] = None
        self._m5_media_adapters: dict[str, M5MediaCapabilityAdapter] = {}

        # Transitional legacy factory. Migrated M5 capabilities never receive or
        # dereference this object; untouched write capabilities still use it.
        def _make_write_broker():
            from webwire.write_broker import WriteBroker

            sb = self._session.sb
            if sb is None:
                raise RuntimeError("Cannot create WriteBroker: session not started")
            return WriteBroker(sb, self._kill)

        self._write_kernel = WriteKernel(
            kill_switch=self._kill,
            risk_registry=DEFAULT_REGISTRY,
            token_bucket=self._bucket,
            dedupe=self._dedupe,
            journal=self._journal,
            write_broker_factory=_make_write_broker,
        )
        self._register_defaults()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> ActionResult:
        """Start the session and install the coherent live M5 authority stack."""
        r = await self._session.start()
        if not r.ok:
            return r
        sb = self._session.sb
        if sb is None:
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                "Session reported started but sb is None",
                failure_category=FailureCategory.BROWSER_CRASH,
            )

        # Restore persisted cookies before first navigation. Persistence is not
        # identity authority; migrated writes still require whoami this run.
        if self._session.session_loaded_state == "pending":
            try:
                await self._session.restore_session_async()
            except Exception as exc:  # noqa: BLE001
                logger.warning("session restore failed: %r", exc)

        try:
            self._install_m5_live_stack(sb)
        except Exception as exc:  # noqa: BLE001
            logger.exception("M5 live execution stack initialization failed")
            try:
                await self._session.stop()
            except Exception as stop_exc:  # noqa: BLE001
                logger.warning("session cleanup after M5 start failure failed: %r", stop_exc)
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                f"M5 live execution stack initialization failed: {exc!r}",
                failure_category=FailureCategory.SECURITY,
            )

        stack = self._m5_stack
        if stack is None:
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                "M5 live execution stack was not retained after initialization",
                failure_category=FailureCategory.SECURITY,
            )
        self._broker = stack.read_broker

        # Transitional legacy safety hydration. Layer 7 will retire the journal
        # safety role only after all write capabilities and RecoveryGuard move.
        try:
            import time as _time

            from webwire.journal import read_recent_write_records

            records = read_recent_write_records(
                self._config.journal_path(), _time.time() - 3600.0,
            )
            n_dedupe = self._dedupe.hydrate_records(records)
            n_budget = self._bucket.hydrate_records(records)
            if n_dedupe or n_budget:
                logger.info(
                    "Hydrated safety stores from journal: %d dedupe entries, %d budget events",
                    n_dedupe,
                    n_budget,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("safety-store hydration failed: %r", exc)

        self._register_defaults()
        try:
            data = r.data or {}
            data["ownership"] = self._session.ownership
            data["session"] = self._session.session_loaded_state
        except Exception:  # noqa: BLE001
            pass
        return r

    async def stop(self) -> ActionResult:
        """Stop only after any sibling Dispatcher invocation leaves the browser."""
        current_task = asyncio.current_task()
        if current_task is self._invoke_lock_owner:
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                "Dispatcher.stop() cannot run from inside an active invocation",
                failure_category=FailureCategory.SECURITY,
            )
        async with self._invoke_lock:
            self._m5_stack = None
            self._m5_canary_adapters.clear()
            self._m5_post_text_adapter = None
            self._m5_reply_adapter = None
            self._m5_quote_adapter = None
            self._m5_delete_adapter = None
            self._m5_media_adapters.clear()
            self._broker = None
            return await self._session.stop()

    # -- invocation ----------------------------------------------------------

    async def invoke(
        self,
        name: str,
        input: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        """Invoke a capability by name through the appropriate trust boundary."""
        current_task = asyncio.current_task()
        if current_task is not self._invoke_lock_owner:
            async with self._invoke_lock:
                self._invoke_lock_owner = current_task
                try:
                    return await self.invoke(name, input)
                finally:
                    self._invoke_lock_owner = None

        input = input or {}
        started_monotonic = time.monotonic()
        trace_id = _new_trace_id()
        capability = self._registry.get(name)

        if self._kill.tripped():
            from webwire.envelope import kill_switched

            result = kill_switched()
            self._journal_write(
                trace_id=trace_id,
                capability=name,
                input=input,
                result=result,
                policy_decision="killed",
                actions=[],
                started_monotonic=started_monotonic,
            )
            return result

        if capability is None:
            result = unsupported_capability(name)
            self._journal_write(
                trace_id=trace_id,
                capability=name,
                input=input,
                result=result,
                policy_decision="unsupported",
                actions=[],
                started_monotonic=started_monotonic,
            )
            return result

        if self._broker is None:
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            result = hard_failure(
                "Dispatcher not started — call await dispatcher.start() first",
                failure_category=FailureCategory.BROWSER_CRASH,
            )
            self._journal_write(
                trace_id=trace_id,
                capability=name,
                input=input,
                result=result,
                policy_decision="denied",
                actions=[],
                started_monotonic=started_monotonic,
            )
            return result

        try:
            if capability.tier == CapabilityTier.WRITE:
                actor = self._session.resolved_handle
                from typing import cast

                from webwire.safety.write_kernel import WriteCapability

                if name in _M5_MIGRATED_CAPABILITIES:
                    if self._m5_stack is None:
                        from super_browser.results.types import FailureCategory

                        from webwire.envelope import hard_failure

                        result = hard_failure(
                            "M5 migrated write is unavailable because the live "
                            "authority stack is not installed",
                            failure_category=FailureCategory.SECURITY,
                        )
                    elif not actor:
                        from super_browser.results.types import FailureCategory

                        from webwire.envelope import hard_failure

                        result = hard_failure(
                            "M5 migrated writes require a whoami-resolved actor identity",
                            failure_category=FailureCategory.SECURITY,
                        )
                    else:
                        if name in _M5_ENGAGEMENT_CAPABILITIES:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_capability_adapter(name, capability),
                            )
                        elif name == _M5_POST_TEXT_CAPABILITY:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_post_text_capability_adapter(capability),
                            )
                        elif name == _M5_REPLY_CAPABILITY:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_reply_capability_adapter(capability),
                            )
                        elif name == _M5_QUOTE_CAPABILITY:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_quote_capability_adapter(capability),
                            )
                        elif name in _M5_MEDIA_CAPABILITIES:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_media_capability_adapter(name, capability),
                            )
                        elif name == _M5_DELETE_CAPABILITY:
                            write_cap = cast(
                                WriteCapability,
                                self._m5_delete_capability_adapter(capability),
                            )
                        else:  # pragma: no cover - guarded by migrated set above
                            raise RuntimeError(f"unsupported M5 migrated capability: {name!r}")
                        result = await self._write_kernel.execute(
                            write_cap,
                            self._broker,
                            input,
                            actor_identity=actor,
                        )
                else:
                    write_cap = cast(WriteCapability, capability)
                    result = await self._write_kernel.execute(
                        write_cap,
                        self._broker,
                        input,
                        actor_identity=actor,
                    )
            else:
                from typing import cast as _cast

                read_cap = _cast("Capability", capability)
                if name == "download_image":
                    from webwire.download_broker import DownloadBroker

                    dl_dir = self._config.state_dir / "downloads"
                    dl_broker = DownloadBroker(
                        self._session.sb,
                        self._kill,
                        dl_dir,
                    )
                    result = await read_cap.run(dl_broker, input)
                else:
                    result = await read_cap.run(self._broker, input)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Capability %r raised", name)
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            result = hard_failure(
                f"Capability {name!r} raised: {exc!r}",
                failure_category=FailureCategory.UNKNOWN,
            )

        if name == "whoami" and result.ok:
            await self._post_whoami_hook(result)

        write_facts = self._write_facts(capability, result)
        self._journal_write(
            trace_id=trace_id,
            capability=name,
            input=input,
            result=result,
            policy_decision="allowed",
            actions=[],
            started_monotonic=started_monotonic,
            capability_tier=(
                capability.tier.value
                if capability.tier == CapabilityTier.WRITE
                else None
            ),
            **write_facts,
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

    def _install_m5_live_stack(self, sb: Any) -> None:
        from webwire.safety.m5_live_runtime import build_live_m5_execution_stack

        self._m5_stack = build_live_m5_execution_stack(
            sb,
            self._kill,
            self._m5_gateway,
            config=self._config,
        )
        self._m5_canary_adapters.clear()
        self._m5_post_text_adapter = None
        self._m5_reply_adapter = None
        self._m5_quote_adapter = None
        self._m5_delete_adapter = None
        self._m5_media_adapters.clear()

    def _m5_capability_adapter(
        self,
        name: str,
        capability: "Capability | WriteCapability",
    ) -> "M5EngagementCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_canary_adapters.get(name)
        if adapter is not None:
            return adapter

        from webwire.safety.m5_capability_adapter import M5EngagementCapabilityAdapter

        adapter = M5EngagementCapabilityAdapter(capability, stack.effect_executor)
        self._m5_canary_adapters[name] = adapter
        return adapter

    def _m5_post_text_capability_adapter(
        self,
        capability: "Capability | WriteCapability",
    ) -> "M5PostTextCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_post_text_adapter
        if adapter is not None:
            return adapter

        from webwire.safety.m5_post_text_adapter import M5PostTextCapabilityAdapter

        adapter = M5PostTextCapabilityAdapter(capability, stack.post_text_executor)
        self._m5_post_text_adapter = adapter
        return adapter

    def _m5_reply_capability_adapter(
        self,
        capability: "Capability | WriteCapability",
    ) -> "M5ReplyCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_reply_adapter
        if adapter is not None:
            return adapter

        from webwire.safety.m5_reply_adapter import M5ReplyCapabilityAdapter

        adapter = M5ReplyCapabilityAdapter(capability, stack.reply_executor)
        self._m5_reply_adapter = adapter
        return adapter

    def _m5_quote_capability_adapter(
        self,
        capability: "Capability | WriteCapability",
    ) -> "M5QuoteCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_quote_adapter
        if adapter is not None:
            return adapter

        from webwire.safety.m5_quote_adapter import M5QuoteCapabilityAdapter

        adapter = M5QuoteCapabilityAdapter(capability, stack.quote_executor)
        self._m5_quote_adapter = adapter
        return adapter

    def _m5_media_capability_adapter(
        self,
        name: str,
        capability: "Capability | WriteCapability",
    ) -> "M5MediaCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_media_adapters.get(name)
        if adapter is not None:
            return adapter

        from webwire.safety.m5_media_adapter import M5MediaCapabilityAdapter

        adapter = M5MediaCapabilityAdapter(capability, stack.media_executor)
        self._m5_media_adapters[name] = adapter
        return adapter

    def _m5_delete_capability_adapter(
        self,
        capability: "Capability | WriteCapability",
    ) -> "M5DeleteCapabilityAdapter":
        stack = self._m5_stack
        if stack is None:
            raise RuntimeError("M5 live execution stack is not installed")
        adapter = self._m5_delete_adapter
        if adapter is not None:
            return adapter

        from webwire.safety.m5_delete_adapter import M5DeleteCapabilityAdapter

        adapter = M5DeleteCapabilityAdapter(capability, stack.delete_executor)
        self._m5_delete_adapter = adapter
        return adapter

    async def _post_whoami_hook(self, result: ActionResult) -> None:
        self._session.mark_authenticated()
        data = result.data if isinstance(result.data, dict) else None
        if data and data.get("handle"):
            self._session.set_resolved_handle(str(data["handle"]))
        try:
            await self._session.checkpoint_session()
        except Exception as exc:  # noqa: BLE001
            logger.warning("post-whoami checkpoint failed: %r", exc)

    @staticmethod
    def _write_facts(
        capability: "Optional[Capability | WriteCapability]",
        result: ActionResult,
    ) -> dict[str, Optional[str]]:
        facts: dict[str, Optional[str]] = {}
        if capability is None or capability.tier != CapabilityTier.WRITE:
            return facts
        data = result.data if isinstance(result.data, dict) else None
        if data and isinstance(data.get("trace"), dict):
            trace_info = data["trace"]
            intent_info = trace_info.get("intent") or {}
            policy_info = data.get("policy") or {}
            facts = {
                "action_type": intent_info.get("action_type"),
                "risk_tier": (
                    intent_info.get("risk_tier") or policy_info.get("risk_tier")
                ),
                "dedupe_key": (
                    intent_info.get("dedupe_key")
                    if trace_info.get("dedupe_recorded") is True
                    else None
                ),
            }
        return facts

    def _register_defaults(self) -> None:
        if self._registered_default:
            return
        self._registry.register(WhoamiCapability())
        self._registry.register(
            HealthCapability(self._kill, self._session, self._config)
        )
        self._registry.register(ReadCapability())
        self._registry.register(ReadProfileCapability())

        from webwire.capabilities.read_thread import ReadThreadCapability

        self._registry.register(ReadThreadCapability())
        from webwire.capabilities.read_search import ReadSearchCapability

        self._registry.register(ReadSearchCapability())
        from webwire.capabilities.bookmark import BookmarkCapability

        self._registry.register(BookmarkCapability())
        from webwire.capabilities.like import LikeCapability

        self._registry.register(LikeCapability())
        from webwire.capabilities.compose_post import ComposePostCapability

        self._registry.register(ComposePostCapability())
        from webwire.capabilities.post_text import PostTextCapability

        self._registry.register(PostTextCapability())
        from webwire.capabilities.reply_post import ReplyPostCapability

        self._registry.register(ReplyPostCapability())
        from webwire.capabilities.quote_post import QuotePostCapability

        self._registry.register(QuotePostCapability())
        from webwire.capabilities.post_photo import PostPhotoCapability

        self._registry.register(PostPhotoCapability())
        from webwire.capabilities.download_image import DownloadImageCapability

        self._registry.register(DownloadImageCapability())
        from webwire.capabilities.reply_photo import ReplyPhotoCapability

        self._registry.register(ReplyPhotoCapability())
        from webwire.capabilities.quote_photo import QuotePhotoCapability

        self._registry.register(QuotePhotoCapability())
        from webwire.capabilities.post_multi_image import PostMultiImageCapability

        self._registry.register(PostMultiImageCapability())
        from webwire.capabilities.reply_multi_image import ReplyMultiImageCapability

        self._registry.register(ReplyMultiImageCapability())
        from webwire.capabilities.quote_multi_image import QuoteMultiImageCapability

        self._registry.register(QuoteMultiImageCapability())
        from webwire.capabilities.delete_post import DeletePostCapability

        self._registry.register(DeletePostCapability())
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
        capability_tier: Optional[str] = None,
        action_type: Optional[str] = None,
        risk_tier: Optional[str] = None,
        dedupe_key: Optional[str] = None,
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
            success_category=(
                result.success_category.value if result.success_category else None
            ),
            failure_category=(
                result.failure_category.value if result.failure_category else None
            ),
            error_message=(err.message if err else None),
            browser_actions=[a.__dict__ for a in actions] if actions else [],
            duration_ms=duration_ms,
            capability_tier=capability_tier,
            action_type=action_type,
            risk_tier=risk_tier,
            dedupe_key=dedupe_key,
        )
        if self._journal.should_capture_screenshot(failed=not result.ok):
            record.screenshot = None
        self._journal.append(record)


def _new_trace_id() -> str:
    import uuid

    return str(uuid.uuid4())


def _redact_target(input: dict[str, Any]) -> Optional[str]:
    for val in input.values():
        if isinstance(val, str) and "://" in val:
            scheme, rest = val.split("://", 1)
            path = rest.split("?", 1)[0].split("#", 1)[0]
            return f"{scheme}://{path}"
    return None


def _redact_input(input: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in input.items():
        if isinstance(value, str) and "://" in value:
            scheme, rest = value.split("://", 1)
            path = rest.split("?", 1)[0].split("#", 1)[0]
            out[key] = f"{scheme}://{path}"
        else:
            out[key] = value
    return out
