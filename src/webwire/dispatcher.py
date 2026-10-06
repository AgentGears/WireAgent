"""Dispatcher — the single entry point for capability execution.

The dispatcher owns the browser session, read broker, kill switch, invocation
journal, write-policy kernel, and the M5 live execution stack. Capability code
never receives the raw SuperBrowser facade.

Supported remote mutations route through M5 scoped authority. ``compose_post``
remains a dry-run WRITE shell with no remote effect. Since Layer 7, the
invocation journal is audit/diagnostic output only; it never hydrates live
mutation safety state.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from webwire.authority_operators import (
        OwnedReconciliationOperatorSession,
    )

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
_M5_DRY_RUN_CAPABILITY = "compose_post"
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
_M5_MIGRATED_CAPABILITIES = (
    _M5_ENGAGEMENT_CAPABILITIES
    | _M5_MEDIA_CAPABILITIES
    | {
        _M5_POST_TEXT_CAPABILITY,
        _M5_REPLY_CAPABILITY,
        _M5_QUOTE_CAPABILITY,
        _M5_DELETE_CAPABILITY,
    }
)


class _NoMutationBroker:
    """Inert broker token for the legacy kernel shell after Layer-5 migration."""

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"legacy mutation broker surface is disabled: {name}")


class Dispatcher:
    """Single entry point: ``await dispatcher.invoke(name, input)``."""

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        session_manager: Optional[SessionManager] = None,
        *,
        enable_ipc: bool = False,  # F-61: becomes True when F-60 transport lands
    ) -> None:
        # M7-RV11 / F-46: resolve state_dir ONCE, before ANY authority-root
        # construction. Every derived path — journal, effect ledger, rules
        # store, lock file — uses this exact canonical absolute domain, so a
        # later CWD change can never split the lock domain from the safety
        # state domain.
        from webwire.offline_recovery import canonical_config

        raw_config = config or WebWireConfig()
        # canonical_config emits the frozen relative-root warning (relative
        # input + resolved absolute domain) when the input is relative.
        self._config = canonical_config(raw_config)
        self._session = session_manager or SessionManager(self._config)
        if session_manager is not None:
            # F-54: an injected manager's RETAINED state root must already
            # BE the frozen canonical absolute domain — not merely resolve
            # equal at validation time. A manager still holding a relative
            # root (or an un-frozen symlinked path) re-resolves its
            # session/profile paths on every use, so a CWD change would
            # split session persistence from the lock/safety domain even
            # though construction-time equality held.
            manager_state = getattr(getattr(session_manager, "_ww_config", None), "state_dir", None)
            if manager_state is None:
                manager_state = getattr(getattr(session_manager, "_config", None), "state_dir", None)
            if manager_state is None:
                manager_state = getattr(session_manager, "state_dir", None)
            if manager_state is not None and Path(manager_state) != self._config.state_dir:
                raise ValueError(
                    "injected session manager retains state root "
                    f"{manager_state!s}, which is not the frozen canonical "
                    f"authority domain {self._config.state_dir}; construct "
                    "the manager from the canonical (absolute) config"
                )
        self._kill = KillSwitch(self._config)
        self._journal = Journal(self._config)
        self._registry = CapabilityRegistry()
        self._broker: Optional[ReadOnlyBroker] = None
        self._registered_default = False
        # Reads/downloads and M5 writes share one browser. Serialize supported
        # Dispatcher invocations so no sibling task can navigate over an owned
        # M5 composer. Same-task re-entry is allowed for trusted orchestration.
        self._invoke_lock = asyncio.Lock()
        # M7 Layer 2: the authority session is the owner-wide lifecycle
        # fence (STARTING → READY → DRAINING → TERMINAL). Ownership is
        # acquired before the session exists and released after it is
        # terminal. A TERMINAL session never becomes active again: build a
        # new Dispatcher for a new owner session.
        self._owner_lock: Optional[Any] = None
        self._authority_session: Optional[Any] = None
        # M7 Layer 4: the IPC server binds after the browser/root and
        # before READY; closes before TERMINAL in the shutdown law.
        self._ipc_server: Optional[Any] = None
        self._ipc_transport: Optional[Any] = None
        self._enable_ipc = enable_ipc
        # The lifecycle fence between start() and stop(). Asyncio (not
        # threading): a threading lock acquired by a waiting coroutine would
        # block the event loop thread itself; this lock is only ever taken
        # inside async lifecycle methods.
        self._lifecycle_gate: Any = asyncio.Lock()
        self._invoke_lock_owner: Optional[asyncio.Task[Any]] = None

        from webwire.safety import (
            DEFAULT_EFFECT_POLICIES,
            DEFAULT_REGISTRY,
            DedupeStore,
            EffectLedger,
            ReconciliationCoordinator,
            RecoveryGuard,
            TokenBucket,
            WriteKernel,
        )
        from webwire.safety.commit_gateway import CommitGateway
        from webwire.safety.execution_models import AuthorizationEpoch
        from webwire.safety.user_rules import RuleStore

        # These two controls are process-local defense in depth. Layer 7 no
        # longer rebuilds either one from the best-effort invocation journal.
        self._dedupe = DedupeStore(ttl_seconds=3600)
        self._bucket = TokenBucket()

        # M5 safety authority exists before a browser is live. The gateway owns
        # the exact same KillSwitch as Dispatcher, so a trip revokes outstanding
        # grant/permit epochs even if no capability is currently running.
        self._m5_ledger = EffectLedger(self._config)
        self._m5_recovery = RecoveryGuard(self._m5_ledger)
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

        # WriteKernel owns the confirmation shell but never receives a live
        # legacy WriteBroker. M5 adapters ignore this inert token and
        # compose_post is a proven dry-run no-op.
        def _make_write_broker() -> _NoMutationBroker:
            return _NoMutationBroker()

        self._write_kernel = WriteKernel(
            kill_switch=self._kill,
            risk_registry=DEFAULT_REGISTRY,
            token_bucket=self._bucket,
            dedupe=self._dedupe,
            journal=self._journal,
            write_broker_factory=_make_write_broker,
            recovery_guard=self._m5_recovery,
            rule_store=RuleStore(self._config.rules_path()),
        )

        # M6 reconciliation is local bookkeeping authority, not a Dispatcher
        # capability. Compose it at the same process authority root so terminal
        # reconciliation advances the exact ConfirmationState that WriteKernel
        # uses for pending human-confirmation tokens, while sharing the canonical
        # M5 CommitGateway lifecycle and composite RecoveryGuard.
        self._m6_reconciliation = ReconciliationCoordinator(
            recovery_guard=self._m5_recovery,
            confirmation_state=self._write_kernel.confirmation_state,
            commit_gateway=self._m5_gateway,
        )
        self._register_defaults()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> ActionResult:
        """Take authority ownership, build the session, start the root.

        The frozen Layer-2 ordering, enforced by the lifecycle gate and the
        AuthoritySession fence (STARTING → READY):

        1. acquire the AuthorityOwnerLock FIRST — before recovery hydration
           and before any browser/session activity, so a losing process
           fails before production browser or safety-write authority;
        2. construct the AuthoritySession in STARTING and register the
           confirmation-epoch revoker, so pending owner-side confirmation
           authority can never survive ownership replacement (F-47);
        3. start the authority root under the gate — a concurrent stop()
           cannot interleave with a half-started root (F-45);
        4. activate (READY) only on success.

        On failure or cancellation, ownership is released only after the
        partially constructed root is demonstrably quiescent — the session
        manager is stopped and its stop must succeed; otherwise the domain
        stays locked fail-closed (F-44). A TERMINAL session never restarts:
        build a new Dispatcher for a new owner session."""
        from super_browser.results.types import FailureCategory

        from webwire.authority import (
            AuthorityBusyError,
            AuthorityOwnerError,
            AuthorityOwnerLock,
        )
        from webwire.authority_session import AuthoritySession
        from webwire.envelope import hard_failure

        async with self._lifecycle_gate:
            if self._authority_session is not None:
                state = self._authority_session.state.value
                if state == "terminal":
                    return hard_failure(
                        "this runtime's authority session is TERMINAL: a "
                        "terminal session never becomes active again — "
                        "construct a new Dispatcher for a new owner session",
                        failure_category=FailureCategory.SECURITY,
                    )
                return hard_failure(
                    "Dispatcher.start() called while already started or starting",
                    failure_category=FailureCategory.SECURITY,
                )
            try:
                owner_lock = AuthorityOwnerLock(self._config.state_dir).acquire()
            except AuthorityBusyError:
                return hard_failure(
                    "authority_busy: another WireAgent runtime owns this state "
                    f"directory ({self._config.state_dir}); "
                    "stop the other runtime before starting a second one",
                    failure_category=FailureCategory.SECURITY,
                )
            except AuthorityOwnerError as exc:
                return hard_failure(
                    f"could not establish authority ownership for this state directory: {exc}",
                    failure_category=FailureCategory.SECURITY,
                )

            session = AuthoritySession(authority_domain=self._config.state_dir)
            # F-47: advancing the confirmation epoch synchronously revokes
            # every pending token; registered as the session revoker it runs
            # before terminalization and before ownership release.
            session.register_revoker(self._write_kernel.confirmation_state.advance_epoch)
            self._owner_lock = owner_lock
            self._authority_session = session
            try:
                result = await self._start_locked()
            except BaseException:
                await self._teardown_failed_start(session)
                raise
            if result.ok:
                if self._enable_ipc:
                    # M7 Layer 4 (F-68): the frozen startup order is
                    #   bind endpoint → READY → start accepting
                    # The transport binds BEFORE session.activate() and
                    # starts accepting AFTER it — a STARTING owner never
                    # serves hellos. Bind/security failure is a startup
                    # failure following the fail-closed law.
                    try:
                        from webwire.authority_ipc_server import (
                            IPC_SERVER_MAX_CONCURRENT,
                            AuthorityIPCServer,
                        )
                        from webwire.authority_ipc_transport import (
                            IPCTransportServer,
                        )

                        self._ipc_server = AuthorityIPCServer(
                            session=session,
                            authority_domain=self._config.state_dir,
                            invoke=self._invoke_admitted,
                        )
                        self._ipc_transport = IPCTransportServer(
                            ipc_server=self._ipc_server,
                            authority_domain=self._config.state_dir,
                            loop=asyncio.get_event_loop(),
                            max_concurrent=IPC_SERVER_MAX_CONCURRENT,  # F-62: one source
                        )
                        self._ipc_transport.bind()  # endpoint exists, not yet accepting
                    except BaseException as exc:
                        logger.exception("IPC endpoint bind failed")
                        await self._teardown_failed_start(session)
                        from super_browser.results.types import FailureCategory

                        from webwire.envelope import hard_failure

                        return hard_failure(
                            f"IPC endpoint bind failed: {exc!r}; startup refused "
                            "(no READY window without the production endpoint)",
                            failure_category=FailureCategory.SECURITY,
                        )
                session.activate()
                # F-68: start accepting only AFTER READY — a STARTING
                # owner never serves hellos to clients.
                if self._enable_ipc and self._ipc_transport is not None:
                    self._ipc_transport.start_accepting()
                try:
                    # `is not None`, not `or {}`: an empty-but-present data
                    # dict must be enriched IN PLACE, not replaced.
                    data = result.data if result.data is not None else {}
                    data["authority_instance_id"] = session.authority_instance_id
                except Exception:  # noqa: BLE001 — diagnostic enrichment only
                    pass
                return result
            await self._teardown_failed_start(session)
            return result

    async def _invoke_admitted(self, name: str, input: dict[str, Any]) -> Any:
        """The IPC server's bounded execution seam into the Dispatcher."""
        return await self._invoke_inner(name, input)

    async def _teardown_failed_start(self, session: Any) -> None:
        """F-44: a failed/cancelled start releases ownership only after the
        partially constructed root is demonstrably quiescent. The session
        manager must stop cleanly; if it cannot, the domain stays locked
        fail-closed and the process owner must investigate."""

        session.revoke_authority()
        try:
            quiesce = await self._session.stop()
        except BaseException as exc:
            self._authority_session = session  # keep for diagnostics
            logger.error(
                "failed-start cleanup could not prove the root quiescent "
                "(session stop raised %r); authority domain stays locked "
                "fail-closed",
                exc,
            )
            return
        if not quiesce.ok:
            self._authority_session = session  # ambiguous: stays locked
            logger.error(
                "failed-start cleanup could not prove the root quiescent "
                "(%s); authority domain stays locked fail-closed",
                getattr(quiesce.error, "message", quiesce),
            )
            return
        # F-52: proven quiescent + revoked — mark the failed session
        # TERMINAL before closing the owner handle, so the Dispatcher never
        # reports a live session it no longer owns.
        # M7 Layer 4: close the IPC transport + endpoint if a bind happened
        # or was partially attempted — no endpoint outlives the owner session.
        if self._ipc_transport is not None:
            try:
                self._ipc_transport.stop()
            except Exception:  # noqa: BLE001 — cleanup during teardown
                logger.warning("IPC transport cleanup during failed start")
            self._ipc_transport = None
        if self._ipc_server is not None:
            self._ipc_server = None
        session.abort_from_starting()
        self._release_authority()

    def _release_authority(self) -> None:
        """Release authority ownership (idempotent; M7 Layer 2).

        M7 Layer 3 semantic invariant, runtime-enforced: deliberate
        ownership release requires a TERMINAL (or absent) authority
        session. A release attempt over a live session refuses and
        RETAINS ownership — the session cannot be orphaned alive, and
        ownership cannot be deliberately released before the session is
        terminal (frozen contract item 3; the lock object itself stays
        encapsulated).

        F-58: ownership state is discarded ONLY AFTER the lock release
        SUCCEEDS. If release raises (Layer 1's qualified ambiguous
        fail-stop state), the handle is retained and the exception
        propagates — a later stop() retries the release rather than
        reporting already-stopped over possibly-still-held ownership."""
        session = self._authority_session
        if session is not None and session.state.value != "terminal":
            raise __import__(
                "webwire.authority_session", fromlist=["AuthoritySessionError"]
            ).AuthoritySessionError(
                "refusing to release authority ownership: the authority "
                f"session is still {session.state.value} (a session must be "
                "TERMINAL before the owner handle closes); ownership retained"
            )
        lock = self._owner_lock
        if lock is None:
            return
        lock.release()  # raises → handle retained, state retained (F-58)
        self._owner_lock = None  # discarded only after successful release

    async def _start_locked(self) -> ActionResult:
        """Start under held authority ownership (caller releases on failure)."""
        # Recovery truth is M5 authority, not diagnostics. Establish it before
        # launching/restoring a browser so corrupt or unreadable effects history
        # cannot accidentally become an empty replay-denial set.
        try:
            self._m5_recovery.hydrate()
        except Exception as exc:  # RecoveryGuardUnavailable; keep boundary fail-closed
            logger.exception("M5 recovery hydration failed")
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                f"M5 recovery hydration failed: {exc!r}",
                failure_category=FailureCategory.SECURITY,
            )

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

        # Layer 7 intentionally performs no journal-backed safety hydration.
        # Dedupe/rate windows begin empty for each process; durable unresolved
        # replay authority has already been established by RecoveryGuard above.
        self._register_defaults()
        try:
            data = r.data or {}
            data["ownership"] = self._session.ownership
            data["session"] = self._session.session_loaded_state
        except Exception:  # noqa: BLE001
            pass
        return r

    async def stop(self) -> ActionResult:
        """The owner shutdown law (frozen Layer 2, in order):

        reject new admission → drain ALL admitted owner work (Dispatcher
        invocations AND reconciliation operator operations) → revoke
        ephemeral confirmation authority → retire the browser/operator
        root → mark the session TERMINAL → close the owner lock LAST.

        The lifecycle gate serializes against start(), so a stop can never
        interleave with a half-started root (F-45), and a TERMINAL session
        never restarts (F-47)."""
        current_task = asyncio.current_task()
        if current_task is self._invoke_lock_owner:
            from super_browser.results.types import FailureCategory

            from webwire.envelope import hard_failure

            return hard_failure(
                "Dispatcher.stop() cannot run from inside an active invocation",
                failure_category=FailureCategory.SECURITY,
            )
        from webwire.envelope import ok_result

        async with self._lifecycle_gate:
            session = self._authority_session
            if session is None or self._owner_lock is None:
                # Never started, or a failed start already cleaned up.
                return ok_result(data={"already_stopped": True})
            if session.state.value == "terminal":
                # F-58: a TERMINAL session with a RETAINED owner lock means
                # a previous release FAILED (the handle is discarded only
                # after a successful release). This stop is a release
                # RETRY, not already-stopped — never report success over
                # possibly-still-held ownership.
                try:
                    self._release_authority()
                except Exception as exc:
                    from super_browser.results.types import FailureCategory

                    from webwire.envelope import hard_failure

                    return hard_failure(
                        "authority release retry failed; ownership state is "
                        f"ambiguous and retained — terminate the process "
                        f"({exc!r})",
                        failure_category=FailureCategory.SECURITY,
                    )
                return ok_result(data={"released_on_retry": True})
            # 1. Reject new admission owner-wide. A session already
            # DRAINING is a RETRY of a failed fail-closed stop: admission
            # is already closed; continue the shutdown law.
            if session.state.value == "ready":
                session.begin_drain()
            # M7 Layer 4: stop accepting new IPC work at DRAINING, and
            # close/unlink the endpoint BEFORE retire/terminalize —
            # already-open connections cannot submit fresh work.
            if self._ipc_server is not None:
                self._ipc_server.begin_drain()
            if self._ipc_transport is not None:
                self._ipc_transport.stop()  # closes connections + endpoint + unlinks
                self._ipc_transport = None
            # 2. Drain all admitted owner work. The invocation lock waits
            #    for in-flight invokes (and blocks new ones); the session
            #    drain waits for admitted reconciliation operations.
            async with self._invoke_lock:
                # The session drain is a blocking condition wait — run it in
                # an executor thread so the event loop stays live: the
                # in-flight admitted work (and its releasers) may need this
                # loop to progress before it can complete.
                drained = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: session.wait_drained(timeout=300.0)
                )
                if not drained:
                    # Fail closed: admission stays closed and ownership
                    # stays held; the caller must resolve the stuck work.
                    from super_browser.results.types import FailureCategory

                    from webwire.envelope import hard_failure

                    return hard_failure(
                        "authority drain timed out with admitted owner work "
                        "still active; ownership is retained fail-closed",
                        failure_category=FailureCategory.SECURITY,
                    )
                # 3. Revoke ephemeral confirmation authority (F-47): every
                #    pending phase-1 token dies with this owner session.
                session.revoke_authority()
                # 4. Retire the operator/browser root.
                self._m5_stack = None
                self._m5_canary_adapters.clear()
                self._m5_post_text_adapter = None
                self._m5_reply_adapter = None
                self._m5_quote_adapter = None
                self._m5_delete_adapter = None
                self._m5_media_adapters.clear()
                self._broker = None
                result = await self._session.stop()
                if not result.ok:
                    # F-49: the browser root is not proven retired. Do NOT
                    # terminalize or release — ownership stays held
                    # fail-closed over an ambiguous mutation-capable root.
                    from super_browser.results.types import FailureCategory

                    from webwire.envelope import hard_failure

                    return hard_failure(
                        "authority release refused: the browser root could "
                        f"not be proven retired ({getattr(result.error, 'message', result)}); "
                        "ownership is retained fail-closed",
                        failure_category=FailureCategory.SECURITY,
                    )
                # 4b. M7 Layer 4: the transport was already stopped at
                # step 1 (DRAINING); the endpoint never outlives the owner.
                self._ipc_server = None
                # 5. Terminal, then 6. release LAST. A release failure
                # (F-58) is fail-stop: the owner handle is RETAINED (the
                # release-retry path above handles a later stop) and the
                # process should be terminated — never report a clean stop
                # over possibly-still-held ownership.
                session.terminalize()
                try:
                    self._release_authority()
                except Exception as exc:
                    from super_browser.results.types import FailureCategory

                    from webwire.envelope import hard_failure

                    return hard_failure(
                        "authority release FAILED; ownership state is "
                        f"ambiguous and retained — terminate the process "
                        f"({exc!r})",
                        failure_category=FailureCategory.SECURITY,
                    )
                return result

    # -- invocation ----------------------------------------------------------

    async def invoke(
        self,
        name: str,
        input: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        """Invoke a capability by name through the appropriate trust boundary.

        M7 Layer 2: an invocation is admitted owner work. While a session
        exists it is admitted through the AuthoritySession — draining or
        terminal sessions refuse new invocations, and stop() drains on the
        same admission count before releasing ownership."""
        from super_browser.results.types import FailureCategory

        from webwire.authority_session import AuthorityAdmissionClosedError
        from webwire.envelope import hard_failure

        session = self._authority_session
        if session is None:
            return await self._invoke_inner(name, input)
        try:
            with session.admit():
                return await self._invoke_inner(name, input)
        except AuthorityAdmissionClosedError as exc:
            return hard_failure(
                f"invocation refused: {exc}",
                failure_category=FailureCategory.SECURITY,
            )

    async def _invoke_inner(
        self,
        name: str,
        input: Optional[dict[str, Any]] = None,
    ) -> ActionResult:
        current_task = asyncio.current_task()
        if current_task is not self._invoke_lock_owner and self._kill.tripped():
            from webwire.envelope import kill_switched

            early_input = input or {}
            started_monotonic = time.monotonic()
            trace_id = _new_trace_id()
            result = kill_switched()
            self._journal_write(
                trace_id=trace_id,
                capability=name,
                input=early_input,
                result=result,
                policy_decision="killed",
                actions=[],
                started_monotonic=started_monotonic,
            )
            return result

        if current_task is not self._invoke_lock_owner:
            async with self._invoke_lock:
                self._invoke_lock_owner = current_task
                try:
                    return await self._invoke_inner(name, input)
                finally:
                    self._invoke_lock_owner = None

        input = input or {}
        started_monotonic = time.monotonic()
        trace_id = _new_trace_id()
        capability = self._registry.get(name)

        # Recheck after acquiring the lock so a trip that races with the wait
        # still dominates capability execution.
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

        policy_decision = "allowed"
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
                        policy_decision = "denied"
                    elif not actor:
                        from super_browser.results.types import FailureCategory

                        from webwire.envelope import hard_failure

                        result = hard_failure(
                            "M5 migrated writes require a whoami-resolved actor identity",
                            failure_category=FailureCategory.SECURITY,
                        )
                        policy_decision = "denied"
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
                elif name == _M5_DRY_RUN_CAPABILITY:
                    write_cap = cast(WriteCapability, capability)
                    result = await self._write_kernel.execute(
                        write_cap,
                        self._broker,
                        input,
                        actor_identity=actor,
                        enforce_recovery_guard=False,
                    )
                else:
                    from super_browser.results.types import FailureCategory

                    from webwire.envelope import hard_failure

                    result = hard_failure(
                        f"unmigrated WRITE capability {name!r} is disabled",
                        failure_category=FailureCategory.SECURITY,
                    )
                    policy_decision = "denied"
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

        if capability.tier == CapabilityTier.WRITE:
            policy_decision = self._audit_policy_decision(policy_decision, result)

        write_facts = self._write_facts(capability, result)
        self._journal_write(
            trace_id=trace_id,
            capability=name,
            input=input,
            result=result,
            policy_decision=policy_decision,
            actions=[],
            started_monotonic=started_monotonic,
            capability_tier=(capability.tier.value if capability.tier == CapabilityTier.WRITE else None),
            **write_facts,
        )
        return result

    # -- accessors -----------------------------------------------------------

    @property
    def capabilities(self) -> list[str]:
        return self._registry.names()

    @property
    def authority_instance_id(self) -> Optional[str]:
        """This runtime's owner-session identity (diagnostic; M7 Layer 3).

        None while this runtime owns no authority domain — a loser that
        never acquired has no instance identity (no READY without
        ownership). After a clean stop the TERMINAL session retains its
        id for diagnostics; a successor acquisition mints a fresh one."""
        session = self._authority_session
        return None if session is None else session.authority_instance_id

    @property
    def kill_switch(self) -> KillSwitch:
        return self._kill

    @property
    def session_manager(self) -> SessionManager:
        return self._session

    def create_reconciliation_operator_session(
        self,
        operator_id: str,
    ) -> "OwnedReconciliationOperatorSession":
        """Create the M6 operator workflow on this runtime's authority state.

        M7 Layer 2 / F-43: reconciliation is owner-side safety work — the
        factory is gated on an ACTIVE (READY) authority session, and every
        operator operation is admitted owner work that stop() drains before
        releasing ownership. Without an active session (never started,
        draining, or terminal) access is refused; use the offline recovery
        owner when no runtime holds the domain."""
        from webwire.authority_operators import (
            OwnedReconciliationOperatorSession,
            require_ready_session,
        )

        session = require_ready_session(self._authority_session)
        return OwnedReconciliationOperatorSession(
            session=session,
            operator_id=operator_id,
            delegate_factory=self._make_reconciliation_operator_session,
        )

    def _make_reconciliation_operator_session(self, operator_id: str) -> Any:
        from webwire.safety.reconciliation_operator import ReconciliationOperatorSession

        return ReconciliationOperatorSession(
            coordinator=self._m6_reconciliation,
            operator_id=operator_id,
        )

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
    def _audit_policy_decision(current: str, result: ActionResult) -> str:
        """Return truthful audit policy metadata without creating authority.

        Dispatcher-level denials keep their explicit value. A result that came
        through WriteKernel exposes its actual policy verdict in ``result.data``;
        use it for audit instead of the old overloaded ``allowed`` marker. If an
        exception/result bypassed normal kernel shaping, fall back to outcome
        truth rather than claiming the policy allowed a failed invocation.
        """

        if current != "allowed":
            return current
        data = result.data if isinstance(result.data, dict) else None
        if data and isinstance(data.get("policy"), dict):
            verdict = data["policy"].get("verdict")
            if isinstance(verdict, str) and verdict:
                return verdict
        return "allowed" if result.ok else "denied"

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
                "risk_tier": (intent_info.get("risk_tier") or policy_info.get("risk_tier")),
                "dedupe_key": (
                    intent_info.get("dedupe_key") if trace_info.get("dedupe_recorded") is True else None
                ),
            }
        return facts

    def _register_defaults(self) -> None:
        if self._registered_default:
            return
        self._registry.register(WhoamiCapability())
        self._registry.register(HealthCapability(self._kill, self._session, self._config))
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
            success_category=(result.success_category.value if result.success_category else None),
            failure_category=(result.failure_category.value if result.failure_category else None),
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
        if key == "confirmation_token":
            # Authority is never audit data (F-50): a live confirmation
            # token is redacted BY KEY — policy gates (a newly installed
            # NEVER, rate limits, dedupe) can deny before the confirmation
            # gate consumes it, so the value may still be live when this
            # record is written.
            out[key] = "<redacted>"
        elif isinstance(value, str) and "://" in value:
            scheme, rest = value.split("://", 1)
            path = rest.split("?", 1)[0].split("#", 1)[0]
            out[key] = f"{scheme}://{path}"
        else:
            out[key] = value
    return out
