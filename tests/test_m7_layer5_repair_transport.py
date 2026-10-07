"""M7 Layer 5 first-review repairs over the REAL transport — F-74 timeout
law, F-73/T41 reconciliation through a REAL Dispatcher, and the true
IPC → Dispatcher → WriteKernel/M5 integration regression.

Platform-neutral: the transport and IPCClient run on POSIX domain sockets
and Windows named pipes alike, so this suite executes in the Windows gate
AND on Linux CI.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from super_browser.results import ActionResult

from webwire.authority_ipc_protocol import IPC_PROTOCOL_VERSION, compute_runtime_build_id
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.config import WebWireConfig
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()

# The M6-valid operator evidence shape (prepare_resolution validates and
# freezes it; minimal ad-hoc shapes die as invalid_evidence).
_M6_EVIDENCE = {
    "basis": "operator-review",
    "observed_at": "2026-10-06T00:00:00+00:00",
    "observations": [{"kind": "operator", "value": "verified"}],
}


def _wire_request(path: str, instance: str, operation: str, payload: dict) -> dict:
    client = IPCClient(path, expected_build_id=BUILD_ID)
    client.connect()
    try:
        return client.request(
            {
                "protocol_version": IPC_PROTOCOL_VERSION,
                "operation": operation,
                "payload": payload,
                "request_id": secrets.token_hex(16),
                "authority_instance_id": instance,
                "runtime_build_id": BUILD_ID,
            }
        )
    finally:
        client.close()


def _wire_request_id(path: str, instance: str, operation: str, payload: dict, request_id: str) -> dict:
    client = IPCClient(path, expected_build_id=BUILD_ID)
    client.connect()
    try:
        return client.request(
            {
                "protocol_version": IPC_PROTOCOL_VERSION,
                "operation": operation,
                "payload": payload,
                "request_id": request_id,
                "authority_instance_id": instance,
                "runtime_build_id": BUILD_ID,
            }
        )
    finally:
        client.close()


async def _wire(path: str, instance: str, operation: str, payload: dict) -> dict:
    """Blocking wire call offloaded off the owner's event loop — calling
    IPCClient.request on the loop thread would deadlock the very handler
    the request is waiting for."""
    return await asyncio.to_thread(_wire_request, path, instance, operation, payload)


# ---------------------------------------------------------------------------
# F-74: transport wait expiry is response uncertainty, never mutation failure
# ---------------------------------------------------------------------------


async def test_timeout_is_request_in_progress_work_survives_and_joins(tmp_path: Path) -> None:
    """The reviewer-specified sequence with an INJECTABLE short wait:
    a mutating request is admitted and blocked; the handler's wait expires
    (response = request_in_progress, NOT a terminal failure); execution is
    still exactly once; the table still owns the live task; a retry with
    the SAME request_id joins it; release → retained terminal response;
    admission returns to zero."""
    session = AuthoritySession(authority_domain=Path("f74"))
    session.register_revoker(lambda: None)
    session.activate()
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()

    release = asyncio.Event()
    executions: list[dict] = []

    async def invoke(name: str, payload: dict) -> Any:
        executions.append(dict(payload))
        await release.wait()
        return ok_result(data={"posted": True})

    ipc = AuthorityIPCServer(
        session=session, authority_domain=tmp_path, invoke=invoke, runtime_build_id=BUILD_ID
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=tmp_path, loop=loop, request_timeout_s=1.0)
    transport.start()
    path = str(transport.endpoint_path or "")
    instance = session.authority_instance_id
    rid = secrets.token_hex(16)

    try:
        # The blocked mutation; the handler wait expires at 1s.
        timed_out = _wire_request_id(path, instance, "post_text", {"text": "uncertain"}, rid)
        assert timed_out["ok"] is False
        assert timed_out["error"]["code"] == "request_in_progress", timed_out
        assert "UNCERTAIN" in timed_out["error"]["message"]

        # The admitted work survived: exactly one execution, still pinned.
        assert len(executions) == 1
        entry = ipc._table._inflight.get(rid)
        assert entry is not None and entry.task is not None and not entry.task.done()

        # The SAME request_id joins the live work and observes the terminal
        # outcome after release.
        result_box: list[dict] = []

        def _joiner() -> None:
            result_box.append(_wire_request_id(path, instance, "post_text", {"text": "uncertain"}, rid))

        joiner = threading.Thread(target=_joiner)
        joiner.start()
        await asyncio.sleep(0.3)
        loop.call_soon_threadsafe(release.set)
        joiner.join(timeout=15)

        assert result_box and result_box[0]["ok"] is True
        assert result_box[0]["data"]["posted"] is True
        assert len(executions) == 1, "joined, never re-executed"

        # The completed frame is retained byte-for-byte for later retries.
        again = _wire_request_id(path, instance, "post_text", {"text": "uncertain"}, rid)
        assert again == result_box[0]

        # Admission fully drained.
        deadline = time.monotonic() + 5
        while session.active_work and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.05)
        assert session.active_work == 0
    finally:
        transport.stop()
        loop.call_soon_threadsafe(loop.stop)
        time.sleep(0.2)  # noqa: ASYNC251
        loop.close()


# ---------------------------------------------------------------------------
# Real-Dispatcher fixtures (F-73/T41 + the integration regression)
# ---------------------------------------------------------------------------


class _StubSB:
    _page = None
    _controller = None


async def _started_ipc_dispatcher(tmp_path: Path, *, stack_installer=None):
    from webwire.dispatcher import Dispatcher
    from webwire.session import SessionManager

    class _RecordingSessionManager(SessionManager):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True
            self._resolved_handle = "@owner"

        async def start(self) -> Any:
            return ok_result(data={})

        async def stop(self) -> Any:
            return ok_result(data={})

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _RecordingSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=sm, enable_ipc=True)  # type: ignore[arg-type]
    if stack_installer is not None:
        dispatcher._install_m5_live_stack = stack_installer  # type: ignore[method-assign]
    else:
        from types import SimpleNamespace

        dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
            lambda sb: setattr(dispatcher, "_m5_stack", SimpleNamespace(read_broker=object()))
        )
    started = await dispatcher.start()
    assert started.ok, getattr(started.error, "message", started)
    return dispatcher


async def test_T41_reconciliation_over_ipc_advances_shared_epoch(tmp_path: Path) -> None:
    """F-73/T41 through a REAL Dispatcher: clients A and B hold write
    confirmation tokens; owner-side reconciliation over IPC (open →
    prepare → confirm → resolve) advances the CANONICAL ConfirmationState
    epoch; both tokens go stale — proof that Layer-5 reconciliation and
    write confirmation share one owner root."""
    from webwire.safety.effect_ledger import EffectLedgerRecord, EffectState
    from webwire.safety.models import RiskTier

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        raw = EffectLedgerRecord(
            effect_id="fx-wire-t41",
            semantic_key="actor|like|post|wire-t41|",
            state=EffectState.EFFECT_UNKNOWN,
            action_type="like",
            intent_hash="intent-wire",
            policy_binding="policy-wire",
            actor_id="actor",
            target_type="post",
            target_id="wire-t41",
            timestamp="2026-10-06T00:00:00+00:00",
        )
        dispatcher._m5_ledger.append_durable(raw)
        dispatcher._m5_recovery.hydrate()

        confirmation_state = dispatcher._write_kernel.confirmation_state
        token_a = confirmation_state.issue(
            intent_hash="ia", risk_tier=RiskTier.PRIVATE_REVERSIBLE, capability_name="bookmark_post"
        )
        token_b = confirmation_state.issue(
            intent_hash="ib", risk_tier=RiskTier.PRIVATE_REVERSIBLE, capability_name="bookmark_post"
        )

        transport = dispatcher._ipc_transport
        path = str(transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        opened = await _wire(path, instance, "reconciliation_open", {"operator_id": "local-admin"})
        assert opened["ok"] is True, opened
        wire_id = opened["data"]["reconciliation_session_id"]

        listed = await _wire(path, instance, "reconciliation_list", {"reconciliation_session_id": wire_id})
        assert listed["ok"] is True, listed
        assert any(t["effect_id"] == "fx-wire-t41" for t in listed["data"]["targets"])

        prepared = await _wire(
            path,
            instance,
            "reconciliation_prepare",
            {
                "reconciliation_session_id": wire_id,
                "effect_id": "fx-wire-t41",
                "verdict": "CONFIRMED_EFFECT",
                "evidence": {
                    "basis": "operator-review",
                    "observed_at": "2026-10-06T00:00:00+00:00",
                    "observations": [{"kind": "operator", "value": "verified"}],
                },
                "evidence_summary": "Operator verified the terminal effect evidence.",
            },
        )
        assert prepared["ok"] is True, prepared
        proposal = prepared["data"]["proposal"]
        assert proposal["effect_id"] == "fx-wire-t41"

        confirmed = await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )
        assert confirmed["ok"] is True, confirmed

        resolved = await _wire(
            path,
            instance,
            "reconciliation_resolve",
            {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
        )
        assert resolved["ok"] is True, resolved
        assert resolved["data"]["resolution"]["confirmation_epoch"] >= 1

        # THE T41 invariant: both clients' tokens are stale under the SAME
        # canonical ConfirmationState the owner's writes use.
        for token, intent in ((token_a, "ia"), (token_b, "ib")):
            _t, reason = confirmation_state.validate_and_consume(
                token.token,
                intent_hash=intent,
                risk_tier=RiskTier.PRIVATE_REVERSIBLE,
                capability_name="bookmark_post",
            )
            assert reason == "stale_confirmation_epoch"

        # And the unresolved effect is now clear for mutation.
        assert dispatcher._m5_recovery.require_clear(raw.semantic_key, refresh=False) is None
    finally:
        await dispatcher.stop()


# ---------------------------------------------------------------------------
# The true IPC → Dispatcher → WriteKernel/M5 integration regression
# ---------------------------------------------------------------------------


class _PortBroker:
    """The browser DOM port for post_text (mirrors the M5 executor test
    double): capture FAILS after submit, so the REAL M5 executor records
    EFFECT_UNKNOWN with reconciliation required."""

    def __init__(self) -> None:
        self.composer_text = ""
        self.submit_calls = 0

    async def fill_composer(self, text: str) -> ActionResult:
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.composer_text})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": 0})

    async def attach_media(self, image_path: str) -> ActionResult:
        raise AssertionError("plain post must not attach media")

    async def close_composer(self) -> ActionResult:
        self.composer_text = ""
        return ok_result(data={"cleanup": "closed"})

    async def click_submit(  # noqa: E501
        self, *, _commit_gate, _precommit_check, _expected_text, _expected_attachments
    ) -> ActionResult:
        self.submit_calls += 1
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"clicked": True})


class _FailingEvidence:
    """Baseline OK; new-post capture FAILS → effect_unknown."""

    async def capture_pre_submit_ids(self) -> ActionResult:
        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(self, pre_submit_ids: set[str], *, exclude_ids=None) -> ActionResult:
        from super_browser.results.types import FailureCategory

        from webwire.envelope import soft_failure

        return soft_failure("capture failed", failure_category=FailureCategory.UNKNOWN)

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        return ok_result(data={"text_matches": True})


async def test_ipc_to_writekernel_uncertain_effect_end_to_end(tmp_path: Path) -> None:
    """The TRUE integration path: a real client socket → the owner's IPC
    pipeline → the REAL Dispatcher → the REAL WriteKernel (preview issues
    a REAL token; confirm consumes it atomically) → the REAL M5 post_text
    executor over a stub DOM port whose capture fails → the wire failure
    frame PRESERVES the uncertain-effect safety state (F-72 end to end).
    """
    from types import SimpleNamespace

    from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
    from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
    from webwire.safety.m5_post_text_executor import M5PostTextExecutor
    from webwire.safety.scoped_authority import ScopedAuthorityBroker

    port = _PortBroker()

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        # Replace the stub stack with the REAL write path over the
        # dispatcher's OWN gateway (the write path consults _m5_stack per
        # invocation; the fresh dispatcher has no cached adapters).
        scoped = ScopedAuthorityBroker(
            port,
            dispatcher._m5_gateway,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        runtime = M5ExecutionRuntime(
            scoped_authority=scoped,
            commit_gateway=dispatcher._m5_gateway,
            policies=DEFAULT_EFFECT_POLICIES,
        )
        executor = M5PostTextExecutor(runtime=runtime, evidence_reader=_FailingEvidence())
        dispatcher._m5_stack = SimpleNamespace(read_broker=object(), post_text_executor=executor)
        transport = dispatcher._ipc_transport
        path = str(transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        # Phase 1 (preview): the REAL WriteKernel mints a REAL token.
        preview = await _wire(path, instance, "post_text", {"text": "live integration post"})
        assert preview["ok"] is True, preview
        kernel_data = preview["data"]["data"]
        token = kernel_data["confirmation_token"]
        assert token and kernel_data["preview"]
        assert dispatcher._write_kernel.confirmation_state._diagnostic_pending_tokens()

        # Phase 2 (confirm): real consumption → real executor → capture
        # failure → EFFECT_UNKNOWN, preserved on the wire (F-72).
        outcome = await _wire(
            path, instance, "post_text", {"text": "live integration post", "confirmation_token": token}
        )
        assert outcome["ok"] is False, outcome
        safety = outcome["safety"]
        assert safety["reconciliation_required"] is True
        assert safety["m5_effect_state"] == "effect_unknown"
        assert safety["semantic_key"] is not None
        assert port.submit_calls == 1

        # The token was consumed exactly once (single-use authority).
        pending = dispatcher._write_kernel.confirmation_state._diagnostic_pending_tokens()
        assert pending[token].consumed is True

        # The durable M5 ledger carries the unresolved effect the wire
        # just reported — the client's reconciliation decision has real
        # backing.
        from webwire.safety.effect_ledger import EffectState

        states = [record.state for record in dispatcher._m5_ledger.read_records()]
        assert EffectState.EFFECT_UNKNOWN in states
    finally:
        await dispatcher.stop()


# ---------------------------------------------------------------------------
# Third-review repairs over the real stack: F-76 faithful display, F-75
# exceptional M6 outcomes, F-78 real pagination
# ---------------------------------------------------------------------------


def _seed_effect(dispatcher: Any, effect_id: str, semantic_key: str, states: list) -> None:
    from webwire.safety.effect_ledger import EffectLedgerRecord

    for index, state in enumerate(states):
        dispatcher._m5_ledger.append_durable(
            EffectLedgerRecord(
                effect_id=effect_id,
                semantic_key=semantic_key,
                state=state,
                action_type="like",
                intent_hash=f"intent-{effect_id}",
                policy_binding=f"policy-{effect_id}",
                actor_id="actor",
                target_type="post",
                target_id=effect_id,
                timestamp=f"2026-10-06T0{index}:00:00+00:00",
            )
        )
    dispatcher._m5_recovery.hydrate()


async def test_F76_real_two_record_display_and_full_lineage(tmp_path: Path) -> None:
    """F-76 against the REAL coordinator: a RESERVED → EFFECT_UNKNOWN
    lifecycle displays the CURRENT state (not the first-record reserved),
    exposes explicit first/current states and timestamps, and carries the
    full M6 lineage — including policy_binding — on the target AND the
    prepare/confirmation display."""
    from webwire.safety.effect_ledger import EffectState

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        _seed_effect(
            dispatcher,
            "fx-f76",
            "actor|like|post|f76|",
            [EffectState.RESERVED, EffectState.EFFECT_UNKNOWN],
        )
        transport = dispatcher._ipc_transport
        path = str(transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        listed = await _wire(path, instance, "reconciliation_list", {"reconciliation_session_id": "?"})
        assert listed["ok"] is False  # unknown session guard sanity
        opened = await _wire(path, instance, "reconciliation_open", {"operator_id": "op"})
        wire_id = opened["data"]["reconciliation_session_id"]
        listed = await _wire(path, instance, "reconciliation_list", {"reconciliation_session_id": wire_id})
        target = listed["data"]["targets"][0]
        assert target["effect_id"] == "fx-f76"
        assert target["state"] == "EFFECT_UNKNOWN", "state must be the CURRENT record"
        assert target["first_state"] == "RESERVED"
        assert target["current_state"] == "EFFECT_UNKNOWN"
        assert target["policy_binding"] == "policy-fx-f76"
        assert target["current_timestamp"] == "2026-10-06T01:00:00+00:00"

        prepared = await _wire(
            path,
            instance,
            "reconciliation_prepare",
            {
                "reconciliation_session_id": wire_id,
                "effect_id": "fx-f76",
                "verdict": "CONFIRMED_EFFECT",
                "evidence": _M6_EVIDENCE,
                "evidence_summary": "verified",
            },
        )
        assert prepared["ok"] is True, prepared
        proposal = prepared["data"]["proposal"]
        assert proposal["policy_binding"] == "policy-fx-f76"
        assert proposal["semantic_key"] == "actor|like|post|f76|"
    finally:
        await dispatcher.stop()


async def test_F75_authority_expired_requires_fresh_confirmation(tmp_path: Path) -> None:
    """F-75: an expired authority is denied BEFORE persistence with a
    STABLE wire state that explicitly requires fresh confirmation — and
    the same proposal CAN be re-confirmed (M6 drops only the expired
    authority, never the proposal)."""
    import time as _time

    from webwire.safety.reconciliation_operator import ReconciliationOperatorSession

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        from webwire.safety.effect_ledger import EffectState

        _seed_effect(dispatcher, "fx-exp", "actor|like|post|exp|", [EffectState.EFFECT_UNKNOWN])
        # Short-lived authority: the REAL coordinator, TTL injected.
        dispatcher._ipc_server._reconciliation_provider = lambda operator_id: ReconciliationOperatorSession(
            coordinator=dispatcher._m6_reconciliation,
            operator_id=operator_id,
            authority_ttl_seconds=0.05,
        )
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]
        proposal = (
            await _wire(
                path,
                instance,
                "reconciliation_prepare",
                {
                    "reconciliation_session_id": wire_id,
                    "effect_id": "fx-exp",
                    "verdict": "CONFIRMED_EFFECT",
                    "evidence": _M6_EVIDENCE,
                    "evidence_summary": "verified",
                },
            )
        )["data"]["proposal"]
        confirmed = await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )
        assert confirmed["ok"] is True

        _time.sleep(0.2)  # noqa: ASYNC251 — the authority expiry wait IS the test
        expired = await _wire(
            path,
            instance,
            "reconciliation_resolve",
            {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
        )
        assert expired["ok"] is False
        assert expired["error"]["code"] == "reconciliation_authority_expired"
        assert "FRESH explicit confirmation" in expired["error"]["message"]

        # The proposal itself survives: a fresh confirmation mints new
        # authority and resolves normally.
        reconfirmed = await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )
        assert reconfirmed["ok"] is True, reconfirmed
        resolved = await _wire(
            path,
            instance,
            "reconciliation_resolve",
            {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
        )
        assert resolved["ok"] is True, resolved
    finally:
        await dispatcher.stop()


async def test_F75_persistence_failure_exposes_frozen_identity(tmp_path: Path) -> None:
    """F-75: an AMBIGUOUS persistence failure after the epoch advanced and
    the authority committed reaches the client as a stable
    reconciliation_persistence_failed state carrying the frozen record
    identity, the ambiguity flag, and the same-proposal continuation
    requirement."""
    from webwire.safety.effect_ledger import EffectState
    from webwire.safety.reconciliation_ledger import ReconciliationLedgerAmbiguousError

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        _seed_effect(dispatcher, "fx-pers", "actor|like|post|pers|", [EffectState.EFFECT_UNKNOWN])
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]
        proposal = (
            await _wire(
                path,
                instance,
                "reconciliation_prepare",
                {
                    "reconciliation_session_id": wire_id,
                    "effect_id": "fx-pers",
                    "verdict": "CONFIRMED_EFFECT",
                    "evidence": _M6_EVIDENCE,
                    "evidence_summary": "verified",
                },
            )
        )["data"]["proposal"]
        await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )

        # Inject an ambiguous durable-append failure on the REAL ledger.
        ledger = dispatcher._m6_reconciliation._reconciliation_ledger
        original_append = ledger.append_durable

        def _ambiguous_append(record: Any) -> None:
            raise ReconciliationLedgerAmbiguousError("injected ambiguous durability")

        ledger.append_durable = _ambiguous_append  # type: ignore[method-assign]
        try:
            failed = await _wire(
                path,
                instance,
                "reconciliation_resolve",
                {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
            )
        finally:
            ledger.append_durable = original_append  # type: ignore[method-assign]

        assert failed["ok"] is False
        assert failed["error"]["code"] == "reconciliation_persistence_failed", failed
        safety = failed["safety"]
        assert safety["ambiguous_durability"] is True
        assert safety["record"]["effect_id"] == "fx-pers"
        assert safety["record"]["semantic_key"] == "actor|like|post|pers|"
        assert "SAME proposal/authority" in safety["continuation_required"]
    finally:
        await dispatcher.stop()


async def test_F75_publication_failure_says_durable_guard_closed(tmp_path: Path) -> None:
    """F-75: a durable-but-unpublished reconciliation reaches the client
    as reconciliation_publication_failed with the frozen identity and
    fact_durable — and the RETRY observes proposal_resolved, the successor
    state the first response already announced."""
    from webwire.safety.effect_ledger import EffectState
    from webwire.safety.recovery_guard import RecoveryGuardUnavailable

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        _seed_effect(dispatcher, "fx-pub", "actor|like|post|pub|", [EffectState.EFFECT_UNKNOWN])
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]
        proposal = (
            await _wire(
                path,
                instance,
                "reconciliation_prepare",
                {
                    "reconciliation_session_id": wire_id,
                    "effect_id": "fx-pub",
                    "verdict": "CONFIRMED_EFFECT",
                    "evidence": _M6_EVIDENCE,
                    "evidence_summary": "verified",
                },
            )
        )["data"]["proposal"]
        await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )

        # Inject a fail-closed guard publication on the REAL coordinator.
        guard = dispatcher._m6_reconciliation._guard
        original_refresh = guard.refresh

        def _closed_refresh() -> Any:
            raise RecoveryGuardUnavailable("injected publication failure")

        guard.refresh = _closed_refresh  # type: ignore[method-assign]
        try:
            failed = await _wire(
                path,
                instance,
                "reconciliation_resolve",
                {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
            )
        finally:
            guard.refresh = original_refresh  # type: ignore[method-assign]

        assert failed["ok"] is False
        assert failed["error"]["code"] == "reconciliation_publication_failed", failed
        safety = failed["safety"]
        assert safety["fact_durable"] is True
        assert safety["guard_publication"] == "failed_closed"
        assert safety["record"]["effect_id"] == "fx-pub"

        # The durable record — not the transport response — is the truth a
        # retry observes: the proposal is now resolved.
        retry = await _wire(
            path,
            instance,
            "reconciliation_resolve",
            {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
        )
        assert retry["ok"] is False
        assert retry["error"]["code"] == "reconciliation"  # proposal_resolved (operator error)
        assert "proposal_resolved" in retry["error"]["message"]
    finally:
        await dispatcher.stop()


async def test_F78_real_pagination_over_multiple_effects(tmp_path: Path) -> None:
    """F-78 against the REAL coordinator: three unresolved effects page
    through the bounded list contract (limit + keyset after_effect_id)."""
    from webwire.safety.effect_ledger import EffectState

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        for suffix in ("a", "b", "c"):
            _seed_effect(
                dispatcher,
                f"fx-p-{suffix}",
                f"actor|like|post|p{suffix}|",
                [EffectState.EFFECT_UNKNOWN],
            )
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id
        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]

        page1 = await _wire(
            path,
            instance,
            "reconciliation_list",
            {"reconciliation_session_id": wire_id, "limit": 2},
        )
        ids1 = [t["effect_id"] for t in page1["data"]["targets"]]
        assert len(ids1) == 2
        assert page1["data"]["total"] == 3
        assert page1["data"]["has_more"] is True

        page2 = await _wire(
            path,
            instance,
            "reconciliation_list",
            {"reconciliation_session_id": wire_id, "limit": 2, "after_effect_id": ids1[-1]},
        )
        ids2 = [t["effect_id"] for t in page2["data"]["targets"]]
        assert len(ids2) == 1
        assert page2["data"]["has_more"] is False
        assert set(ids1) | set(ids2) == {f"fx-p-{s}" for s in "abc"}
    finally:
        await dispatcher.stop()


# ---------------------------------------------------------------------------
# Fourth-review regressions over the real stack: F-79 committed-continuation
# close protection; F-81 true-total pagination through the bounded query
# ---------------------------------------------------------------------------


async def test_F79_close_refused_until_committed_continuation_redriven(tmp_path: Path) -> None:
    """F-79: after an ambiguous persistence failure the wire session holds
    the ONLY client-reachable reference to the exact committed authority.
    close is REFUSED (reconciliation_continuation_required); the session
    survives; a retry of the FAILED request_id returns the retained
    failure (it cannot perform the continuation); a FRESH request_id
    re-drives the SAME proposal and succeeds; only then does close
    succeed."""
    from webwire.safety.effect_ledger import EffectState
    from webwire.safety.reconciliation_ledger import ReconciliationLedgerAmbiguousError

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        _seed_effect(dispatcher, "fx-f79", "actor|like|post|f79|", [EffectState.EFFECT_UNKNOWN])
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]
        proposal = (
            await _wire(
                path,
                instance,
                "reconciliation_prepare",
                {
                    "reconciliation_session_id": wire_id,
                    "effect_id": "fx-f79",
                    "verdict": "CONFIRMED_EFFECT",
                    "evidence": _M6_EVIDENCE,
                    "evidence_summary": "verified",
                },
            )
        )["data"]["proposal"]
        await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )

        # Inject the ambiguous persistence failure for ONE resolve.
        ledger = dispatcher._m6_reconciliation._reconciliation_ledger
        original_append = ledger.append_durable

        def _ambiguous_append(record: Any) -> None:
            raise ReconciliationLedgerAmbiguousError("injected ambiguous durability")

        ledger.append_durable = _ambiguous_append  # type: ignore[method-assign]
        try:
            failed = await _wire(
                path,
                instance,
                "reconciliation_resolve",
                {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
            )
        finally:
            ledger.append_durable = original_append  # type: ignore[method-assign]
        assert failed["error"]["code"] == "reconciliation_persistence_failed"
        assert "FRESH request_id" in failed["safety"]["continuation_required"]

        # The wire session now holds a committed-but-unconsumed authority:
        # close is REFUSED and the session survives.
        refused = await _wire(path, instance, "reconciliation_close", {"reconciliation_session_id": wire_id})
        assert refused["ok"] is False
        assert refused["error"]["code"] == "reconciliation_continuation_required"
        still = await _wire(
            path, instance, "reconciliation_list", {"reconciliation_session_id": wire_id, "limit": 1}
        )
        assert still["ok"] is True, "the session must survive the refused close"

        # A NEW open is NOT a workaround for the continuation either — but
        # more importantly the SAME session's exact authority re-drives:
        # a retry with the FAILED request_id returns the retained failure
        # (cannot continue), so a fresh id is required. Re-drive now.
        resolved = await _wire(
            path,
            instance,
            "reconciliation_resolve",
            {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
        )
        assert resolved["ok"] is True, resolved
        assert resolved["data"]["resolution"]["confirmation_epoch"] >= 1

        # Continuation complete: close now succeeds.
        closed = await _wire(path, instance, "reconciliation_close", {"reconciliation_session_id": wire_id})
        assert closed["ok"] is True and closed["data"]["closed"] is True
    finally:
        await dispatcher.stop()


async def test_F79_same_request_id_retry_returns_retained_persistence_failure(tmp_path: Path) -> None:
    """The §10.5 corollary the F-75 response now states: retrying the
    FAILED resolve with the SAME request_id returns the retained failure
    frame byte-identically — it cannot perform the continuation; only a
    FRESH request_id executes the re-drive."""
    import json as _json
    import secrets as _secrets

    from webwire.authority_ipc_framing import encode_json_frame as _enc
    from webwire.authority_ipc_framing import parse_frame_header as _parse
    from webwire.safety.effect_ledger import EffectState
    from webwire.safety.reconciliation_ledger import ReconciliationLedgerAmbiguousError

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        _seed_effect(dispatcher, "fx-f79b", "actor|like|post|f79b|", [EffectState.EFFECT_UNKNOWN])
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]
        proposal = (
            await _wire(
                path,
                instance,
                "reconciliation_prepare",
                {
                    "reconciliation_session_id": wire_id,
                    "effect_id": "fx-f79b",
                    "verdict": "CONFIRMED_EFFECT",
                    "evidence": _M6_EVIDENCE,
                    "evidence_summary": "verified",
                },
            )
        )["data"]["proposal"]
        await _wire(
            path,
            instance,
            "reconciliation_confirm",
            {
                "reconciliation_session_id": wire_id,
                "proposal_id": proposal["proposal_id"],
                "confirmation_text": proposal["confirmation_text"],
            },
        )

        def _resolve_frame(rid: str) -> bytes:
            return _enc(
                {
                    "protocol_version": 1,
                    "operation": "reconciliation_resolve",
                    "payload": {
                        "reconciliation_session_id": wire_id,
                        "proposal_id": proposal["proposal_id"],
                    },
                    "request_id": rid,
                    "authority_instance_id": instance,
                    "runtime_build_id": dispatcher._ipc_server._runtime_build_id,
                }
            )

        async def _drive(frame: bytes) -> dict:
            header, consumed = _parse(frame)
            return _json.loads((await dispatcher._ipc_server.process_request(header, frame[consumed:]))[8:])

        # 1. The resolve FAILS under an injected ambiguous append.
        ledger = dispatcher._m6_reconciliation._reconciliation_ledger
        original_append = ledger.append_durable

        def _ambiguous_append(record: Any) -> None:
            raise ReconciliationLedgerAmbiguousError("injected ambiguous durability")

        rid = _secrets.token_hex(16)
        frame = _resolve_frame(rid)
        ledger.append_durable = _ambiguous_append  # type: ignore[method-assign]
        try:
            failed = await _drive(frame)
        finally:
            ledger.append_durable = original_append  # type: ignore[method-assign]
        assert failed["error"]["code"] == "reconciliation_persistence_failed"

        # 2. SAME request_id: the retained failure frame, byte-identical —
        #    no continuation.
        assert await _drive(frame) == failed

        # 3. FRESH request_id: the exact committed authority re-drives and
        #    resolves terminally.
        resolved = await _drive(_resolve_frame(_secrets.token_hex(16)))
        assert resolved["ok"] is True, resolved
    finally:
        await dispatcher.stop()


async def test_F81_true_total_and_remaining_pagination(tmp_path: Path) -> None:
    """F-81 via the real bounded query: `total` is the TRUE total
    unresolved count (cursor-independent); `remaining` is after-cursor."""
    from webwire.safety.effect_ledger import EffectState

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        for suffix in ("a", "b", "c"):
            _seed_effect(
                dispatcher,
                f"fx-q-{suffix}",
                f"actor|like|post|q{suffix}|",
                [EffectState.EFFECT_UNKNOWN],
            )
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id
        wire_id = (await _wire(path, instance, "reconciliation_open", {"operator_id": "op"}))["data"][
            "reconciliation_session_id"
        ]

        page1 = await _wire(
            path,
            instance,
            "reconciliation_list",
            {"reconciliation_session_id": wire_id, "limit": 2},
        )
        ids1 = [t["effect_id"] for t in page1["data"]["targets"]]
        assert len(ids1) == 2
        assert page1["data"]["total"] == 3
        assert page1["data"]["remaining"] == 3
        assert page1["data"]["has_more"] is True

        page2 = await _wire(
            path,
            instance,
            "reconciliation_list",
            {"reconciliation_session_id": wire_id, "limit": 2, "after_effect_id": ids1[-1]},
        )
        ids2 = [t["effect_id"] for t in page2["data"]["targets"]]
        assert len(ids2) == 1
        assert page2["data"]["total"] == 3, "the TRUE total is cursor-independent"
        assert page2["data"]["remaining"] == 1
        assert page2["data"]["has_more"] is False
        assert set(ids1) | set(ids2) == {f"fx-q-{s}" for s in "abc"}
    finally:
        await dispatcher.stop()

# ---------------------------------------------------------------------------
# F-81 final: the paging single pass — instrumented bounded enumeration
# ---------------------------------------------------------------------------


async def test_F81_single_pass_no_resort_no_full_target_materialization(tmp_path: Path) -> None:
    """F-81 (final): over a large ALREADY-SORTED projection, one page
    request constructs AT MOST `limit` ReconciliationTarget wrappers and
    never invokes sorted() — no unresolved-id list, no candidate list, no
    re-sort, no lookup dict. Functional total/remaining/has_more checks
    remain intact."""
    from unittest.mock import patch as _patch

    import webwire.safety.reconciliation_coordinator as coordinator_module

    dispatcher = await _started_ipc_dispatcher(tmp_path)
    try:
        count = 500

        class _FakeProjection:
            def __init__(self, index: int) -> None:
                self.effect_id = f"fx-{index:04d}"
                self.unresolved = True
                self.first_record = type("R", (), {"state": type("S", (), {"value": "EFFECT_UNKNOWN"})()})()
                self.last_record = self.first_record

        # A large canonical snapshot, pre-sorted exactly as the real
        # projector guarantees — so no sort is owed to anyone.
        sorted_projection = [_FakeProjection(i) for i in range(count)]
        dispatcher._m6_reconciliation._guard.projector.project = (  # type: ignore[method-assign]
            lambda: sorted_projection
        )

        constructed: list[int] = []

        class _CountingTarget(coordinator_module.ReconciliationTarget):
            def __new__(cls, *args: Any, **kwargs: Any) -> Any:
                constructed.append(1)
                return super().__new__(cls)

        with (
            _patch.object(coordinator_module, "ReconciliationTarget", _CountingTarget),
            _patch("builtins.sorted") as sorted_spy,
        ):
            page, total, remaining, has_more = dispatcher._m6_reconciliation.list_targets_page(
                limit=1, after_effect_id="fx-0009"
            )

        assert total == count
        assert remaining == count - 10
        assert has_more is True
        assert [t.effect_id for t in page] == ["fx-0010"], "the next page after the cursor"
        assert len(constructed) == 1, "exactly limit wrappers constructed — never the full set"
        assert sorted_spy.call_count == 0, "the single pass adds no sort over the sorted snapshot"

        # A first page (no cursor) is equally bounded.
        constructed.clear()
        with (
            _patch.object(coordinator_module, "ReconciliationTarget", _CountingTarget),
            _patch("builtins.sorted") as sorted_spy,
        ):
            first_page, total, remaining, has_more = dispatcher._m6_reconciliation.list_targets_page(limit=3)
        assert [t.effect_id for t in first_page] == ["fx-0000", "fx-0001", "fx-0002"]
        assert (total, remaining, has_more) == (count, count, True)
        assert len(constructed) == 3
        assert sorted_spy.call_count == 0
    finally:
        await dispatcher.stop()
