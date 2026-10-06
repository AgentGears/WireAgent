"""M7 Layer 4 — the authority IPC server: the owner-side security boundary.

The processing pipeline (frozen contract; the Dispatcher is reachable ONLY
after every earlier gate passes):

    1. frame decode (bounded, non-executable, strict UTF-8 JSON)
    2. handshake compatibility (exact protocol + exact runtime_build_id)
    3. operation allowlist (the frozen advertised surface: five pure
       reads/health, whoami, six non-file writes, and the reconciliation
       operator ops — media/multi-image/download/compose die HERE, before
       the Dispatcher is reachable; M7-T66)
    4. strict schema validation + normalization
    5. canonical request identity (deterministic, post-schema)
    6. owner admission via AuthoritySession.admit(expected_instance_id=)
       — stale instances lose atomically against drain/admission
    7. ONLY THEN Dispatcher.invoke() under the session admission

Layer-4 constraints implemented here:
- bounded connection/request concurrency with saturation refusal
- client disconnect does NOT cancel the admitted owner task
- the session admission spans the COMPLETE Dispatcher invocation, so
  shutdown drains these reads before releasing ownership
- the endpoint binds AFTER the browser/root is built and BEFORE READY;
  bind/security failure is a startup failure following the fail-closed
  teardown law; shutdown closes/unlinks the endpoint BEFORE TERMINAL and
  the owner-lock release LAST.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path
from typing import Any, Callable, Optional

from webwire.authority_ipc_framing import (
    FrameHeader,
    encode_json_frame,
)
from webwire.authority_ipc_protocol import (
    IPC_PROTOCOL_VERSION,
    IPC_SUPPORTED_OPERATIONS,
    AuthorityHello,
    IPCProtocolError,
    IPCSchemaError,
    canonical_request_identity,
    compute_runtime_build_id,
    validate_and_normalize_request,
)
from webwire.authority_ipc_request_table import RetainedRequestTable
from webwire.authority_session import (
    AuthorityAdmissionClosedError,
    AuthoritySession,
    AuthorityStaleInstanceError,
)

logger = logging.getLogger(__name__)

__all__ = ["AuthorityIPCServer", "IPC_SERVER_MAX_CONCURRENT", "IPC_DRAINING_MESSAGE"]


IPC_SERVER_MAX_CONCURRENT = 4  # bounded connection concurrency
IPC_DRAINING_MESSAGE = "authority session is draining: new IPC work refused"

# F-72: the safety-critical outcome fields a FAILED ActionResult must
# project onto the wire. A client uses these to distinguish "proven
# denial / no effect" from "external effect may have occurred" and to
# decide whether reconciliation is required — losing them at the IPC
# boundary would be unsafe now that mutations are remotely callable.
_SAFETY_SCALAR_FIELDS = (
    "public_side_effect",
    "reconciliation_required",
    "m5_effect_state",
    "terminal_persistence_failed",
    "semantic_key",
    "dedupe_key",
    "posted_url",
    "posted_post_id",
    "failure_code",
    "status",
)
_PROJECTION_MAX_DEPTH = 4
_PROJECTION_MAX_ITEMS = 32
_PROJECTION_MAX_STR = 512


def _json_safe(value: Any, depth: int = 0) -> Any:
    """Recursively bound and JSON-sanitize one value.

    Non-finite floats become None (the strict encoder would reject them);
    over-long strings truncate; over-deep/over-wide containers truncate
    with a marker; unknown object types reduce to a bounded repr (StrEnum
    members are strings, so verdicts/tiers keep their exact values)."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        import math

        return value if math.isfinite(value) else None
    if isinstance(value, str):
        return value[:_PROJECTION_MAX_STR]
    if isinstance(value, (list, tuple)):
        if depth >= _PROJECTION_MAX_DEPTH:
            return f"<{len(value)} items truncated>"
        return [_json_safe(v, depth + 1) for v in value[:_PROJECTION_MAX_ITEMS]]
    if isinstance(value, dict):
        if depth >= _PROJECTION_MAX_DEPTH:
            return {"<truncated>": True}
        out: dict[str, Any] = {}
        for key, item in list(value.items())[:_PROJECTION_MAX_ITEMS]:
            out[str(key)[:128]] = _json_safe(item, depth + 1)
        return out
    return repr(value)[:_PROJECTION_MAX_STR]


def _safety_projection(result: Any) -> dict[str, Any]:
    """The bounded, JSON-safe projection of a FAILED ActionResult's
    safety state (F-72). Never stringifies the whole result, never
    exposes raw authority objects — only the allowlisted outcome
    scalars, a narrowed policy verdict, the final trace stages, and a
    bounded copy of the capability's own result data."""
    projection: dict[str, Any] = {}
    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return projection
    for key in _SAFETY_SCALAR_FIELDS:
        if key in data:
            projection[key] = _json_safe(data[key])
    # The M5 execution payload nests under "data" — hoist the allowlisted
    # safety scalars to the top of the projection so the wire contract has
    # one canonical place for them regardless of the kernel's nesting.
    nested_payload = data.get("data")
    if isinstance(nested_payload, dict):
        for key in _SAFETY_SCALAR_FIELDS:
            if key in nested_payload and key not in projection:
                projection[key] = _json_safe(nested_payload[key])
    policy = data.get("policy")
    if isinstance(policy, dict):
        projection["policy"] = {
            name: _json_safe(policy[name])
            for name in ("verdict", "blocked_by", "risk_tier", "reason")
            if name in policy
        }
    trace = data.get("trace")
    if isinstance(trace, dict):
        stages = trace.get("stages")
        if isinstance(stages, (list, tuple)):
            projection["trace_stages"] = _json_safe(list(stages)[-16:])
        # The kernel's intent trace carries the dedupe/semantic key —
        # the very key the RecoveryGuard blocks replay on. Surface it
        # when the payload itself did not.
        intent = trace.get("intent")
        if isinstance(intent, dict) and "semantic_key" not in projection:
            dedupe_key = intent.get("dedupe_key")
            if dedupe_key:
                projection["semantic_key"] = _json_safe(dedupe_key)
    nested = data.get("data")
    if nested is not None:
        projection["data"] = _json_safe(nested)
    return projection


class IPCRequestOutcome:
    """The bounded result envelope sent back over the wire."""

    @staticmethod
    def ok(data: Any) -> dict[str, Any]:
        return {"ok": True, "data": data}

    @staticmethod
    def error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}

    @staticmethod
    def failure(code: str, message: str, safety: dict[str, Any]) -> dict[str, Any]:
        """A FAILED capability outcome. ``safety`` is the F-72 projection:
        the bounded safety state (side-effect/reconciliation/effect-state
        facts, policy verdict, final trace stages) a client needs to
        distinguish an uncertain external mutation from a clean denial."""
        envelope = IPCRequestOutcome.error(code, message)
        envelope["safety"] = safety
        return envelope


# F-73: owner-side, bounded, instance-bound reconciliation operator
# sessions over IPC (frozen §14.3). The wire session holds the
# OwnedReconciliationOperatorSession and the minted ReconciliationAuthority
# objects PRIVATE to the owner — the client only ever sees opaque ids,
# serializable display data, and terminal results.
IPC_MAX_RECONCILIATION_SESSIONS = 4


class _ReconciliationWireSession:
    """One logical operator session: instance-bound, process-local, and
    revoked at drain/restart (the registry is cleared)."""

    def __init__(self, *, instance_id: str, operator_session: Any) -> None:
        import secrets

        self.id = secrets.token_hex(16)
        self.instance_id = instance_id
        self.operator_session = operator_session
        # proposal_id -> serialized display proposal (frozen at prepare)
        self.proposals: dict[str, dict[str, Any]] = {}
        # proposal_id -> the MINTED ReconciliationAuthority (owner-private;
        # the client confirms by exact text and addresses it by id only)
        self.authorities: dict[str, Any] = {}


class AuthorityIPCServer:
    """The owner-side IPC boundary, lifecycle-integrated.

    ``start()`` binds the secured endpoint — called by the Dispatcher
    AFTER the owner lock, the session (STARTING), hydration, and the
    browser/root, and BEFORE the session activates to READY. ``stop()``
    closes the listener and endpoint — called BEFORE terminalize/release
    in the shutdown law.
    """

    def __init__(
        self,
        *,
        session: AuthoritySession,
        authority_domain: Path,
        invoke: Any,  # async callable(name, input) -> ActionResult
        runtime_build_id: Optional[str] = None,
        kill_probe: Optional[Callable[[], bool]] = None,
        recovery_probe: Optional[Callable[[], bool]] = None,
        reconciliation_provider: Optional[Callable[[str], Any]] = None,
    ) -> None:
        if runtime_build_id is None:
            runtime_build_id = compute_runtime_build_id()
        self._session = session
        self._authority_domain = authority_domain
        self._invoke = invoke
        self._runtime_build_id = runtime_build_id
        self._active_requests = 0
        self._draining = False
        # F-65: live producers for the two abnormal §10.3 wire states. The
        # Dispatcher wires kill_probe to KillSwitch.tripped and recovery_probe
        # to "not RecoveryGuard.status().available" — the same signals the
        # enforcement path already consults, never a second source of truth.
        # (F-62: connection capacity lives in ONE place — the transport's
        # semaphore, fed by IPC_SERVER_MAX_CONCURRENT via the Dispatcher.)
        self._kill_probe = kill_probe
        self._recovery_probe = recovery_probe
        # M7 Layer 5 — the retained request table (frozen §10.5): the
        # process-local dedupe state for THIS owner instance. It dies with
        # the owner; a successor starts empty (M7-T26).
        self._table = RetainedRequestTable()
        # F-73 (§14.3): reconciliation routes through THIS owner. The
        # provider mints OwnedReconciliationOperatorSessions (every
        # operation owner-admitted); the wire registry is bounded,
        # instance-bound, and cleared at drain — restart destroys every
        # logical session and any uncommitted operator authority.
        self._reconciliation_provider = reconciliation_provider
        self._reconciliation_sessions: dict[str, _ReconciliationWireSession] = {}

    @property
    def active_requests(self) -> int:
        return self._active_requests

    # -- lifecycle (drain flag only; the TRANSPORT owns the endpoint) -----

    def begin_drain(self) -> None:
        """Stop accepting new IPC work (called at DRAINING). Already-open
        connections cannot submit fresh work after this point."""
        self._draining = True
        # §14.3: operator sessions are closed by drain — every logical
        # reconciliation session and its uncommitted operator authority
        # is revoked here, not merely at process exit.
        self._reconciliation_sessions.clear()

    # -- request processing (the security pipeline) -------------------------

    def _hello(self) -> AuthorityHello:
        return AuthorityHello(
            protocol_version=IPC_PROTOCOL_VERSION,
            runtime_version=sys.version.split()[0],
            runtime_build_id=self._runtime_build_id,
            authority_instance_id=self._session.authority_instance_id,
            authority_domain=str(self._authority_domain),
            supported_operations=IPC_SUPPORTED_OPERATIONS,
            # F-65: the WIRE state is the frozen §10.3 diagnostic vocabulary
            # (ready/draining/killed/recovery_unavailable), NOT the internal
            # AuthoritySession lifecycle. A STARTING owner never serves
            # hellos (the endpoint binds during STARTING but the accept
            # loop starts at READY); terminal maps to draining for wire
            # purposes (a terminal owner accepts nothing).
            lifecycle_state=self._wire_state(),
        )

    @staticmethod
    def _WIRE_STATES() -> frozenset[str]:
        return frozenset({"ready", "draining", "killed", "recovery_unavailable"})

    def _wire_state(self) -> str:
        """Map live owner signals to the frozen wire vocabulary.

        Priority: killed > recovery_unavailable > ready > draining. The
        safety signals outrank the lifecycle ones — a client connecting
        during the drain race window still deserves to learn that the
        kill switch is tripped or that composite recovery truth is
        unavailable. A probe that raises is treated as "not signalled"
        (fail-open on the DIAGNOSTIC only; the enforcement paths behind
        these signals stay fail-closed independently)."""
        if self._kill_probe is not None:
            try:
                if self._kill_probe():
                    return "killed"
            except Exception:  # noqa: BLE001 - diagnostic probe must not break hello
                logger.exception("kill_probe raised; treating as not signalled")
        if self._recovery_probe is not None:
            try:
                if self._recovery_probe():
                    return "recovery_unavailable"
            except Exception:  # noqa: BLE001 - diagnostic probe must not break hello
                logger.exception("recovery_probe raised; treating as not signalled")
        internal = self._session.state.value
        if internal == "ready":
            return "ready"
        # draining / starting / terminal all mean "not accepting" on the
        # wire; the §10.3 vocabulary has no "starting" or "terminal".
        return "draining"

    async def process_request(self, raw_header: FrameHeader, raw_payload: bytes) -> bytes:
        """The full security pipeline for one framed request.

        Returns the framed response. The Dispatcher is reachable only
        after: frame decode → handshake fields → operation allowlist →
        schema validation → stale-instance admission."""
        from webwire.authority_ipc_framing import decode_json_frame
        from webwire.authority_ipc_protocol import IPC_MAX_REQUEST_BYTES

        # F-62: the 64KiB REQUEST ceiling is enforced from the announced
        # header BEFORE any JSON decoding or allocation — the 1MiB frame
        # ceiling alone would allow oversized request payloads.
        if raw_header.length > IPC_MAX_REQUEST_BYTES:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "request_oversized",
                    f"announced request length {raw_header.length} exceeds the "
                    f"{IPC_MAX_REQUEST_BYTES}-byte request ceiling",
                ),
                is_response=True,
            )
        try:
            request = decode_json_frame(raw_header, raw_payload)
        except IPCProtocolError as exc:
            return encode_json_frame(IPCRequestOutcome.error("protocol", str(exc)), is_response=True)

        # Handshake/compatibility fields on the request envelope.
        # F-63: int-but-not-bool — Python's == lets True==1 and 1.0==1
        # through a plain != comparison; the exact gate must reject both.
        protocol_version = request.get("protocol_version")
        if (
            isinstance(protocol_version, bool)
            or not isinstance(protocol_version, int)
            or protocol_version != IPC_PROTOCOL_VERSION
        ):
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "protocol_mismatch",
                    f"protocol version {protocol_version!r} is not {IPC_PROTOCOL_VERSION}",
                ),
                is_response=True,
            )
        # F-63: runtime_build_id is REQUIRED — omitting it must not bypass
        # the exact-build gate.
        client_build_id = request.get("runtime_build_id")
        if not isinstance(client_build_id, str) or not client_build_id:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "missing_build_id",
                    "runtime_build_id is required (exact-build compatibility cannot be bypassed by omission)",
                ),
                is_response=True,
            )
        if client_build_id != self._runtime_build_id:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "build_mismatch",
                    "runtime build mismatch: the owner and client are different artifacts",
                ),
                is_response=True,
            )

        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in IPC_SUPPORTED_OPERATIONS:
            # The allowlist: everything outside the frozen advertised
            # surface — media/multi-image writes, artifacts, download,
            # compose, and any non-capability name — dies HERE, before
            # the Dispatcher is reachable.
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "unsupported_operation",
                    f"operation {operation!r} is not in the Layer-4 IPC "
                    f"surface {sorted(IPC_SUPPORTED_OPERATIONS)}",
                ),
                is_response=True,
            )

        payload = request.get("payload", {})
        try:
            normalized = validate_and_normalize_request(operation, payload)
        except IPCSchemaError as exc:
            return encode_json_frame(IPCRequestOutcome.error("schema", str(exc)), is_response=True)

        # Canonical identity (deterministic, post-schema) — computed for the
        # trace, never authority (request_id carries no special power).
        _identity = canonical_request_identity(operation, normalized)

        # F-63: request_id is required as a HIGH-ENTROPY identifier (at
        # least 128 random bits, hex-encoded: 32 hex chars from
        # secrets.token_hex(16)) — not an arbitrary 8-character string.
        # Routing identity only; NOT the Layer-5 retained request table.
        request_id = request.get("request_id")
        if (
            not isinstance(request_id, str)
            or len(request_id) < 32
            or len(request_id) > 128
            or not all(c in "0123456789abcdef" for c in request_id)
        ):
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "schema",
                    "request_id must be a hex string of at least 32 "
                    "characters (128+ bits of entropy, e.g. "
                    "secrets.token_hex(16))",
                ),
                is_response=True,
            )

        # F-63: the ENTIRE envelope is strict — EXACT EQUALITY with the
        # six frozen keys (both unknown AND missing fields reject).
        _ENVELOPE_KEYS = frozenset(
            {
                "protocol_version",
                "runtime_build_id",
                "authority_instance_id",
                "request_id",
                "operation",
                "payload",
            }
        )
        _envelope_actual = set(request.keys())
        _envelope_unknown = _envelope_actual - _ENVELOPE_KEYS
        _envelope_missing = _ENVELOPE_KEYS - _envelope_actual
        if _envelope_unknown or _envelope_missing:
            _detail = []
            if _envelope_unknown:
                _detail.append(f"unknown: {sorted(_envelope_unknown)}")
            if _envelope_missing:
                _detail.append(f"missing: {sorted(_envelope_missing)}")
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "schema",
                    "envelope must have exactly the six frozen keys; " + "; ".join(_detail),
                ),
                is_response=True,
            )

        # Stale-instance admission: the Layer-3 primitive, not a pre-check.
        expected_instance = request.get("authority_instance_id")
        if not isinstance(expected_instance, str) or not expected_instance:
            return encode_json_frame(
                IPCRequestOutcome.error("missing_instance", "authority_instance_id is required"),
                is_response=True,
            )
        # M7 Layer 5 fast-path: a client addressing a DEAD owner instance
        # is rejected as stale BEFORE any table state is consulted — an
        # old token (or reused id) never learns or reaches anything under
        # the current owner. The atomic admit below re-checks the race.
        if expected_instance != self._session.authority_instance_id:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "stale_authority_instance",
                    "the owner instance this request addressed is no longer the active owner",
                ),
                is_response=True,
            )
        # M7 Layer 5 — the retained request table (frozen §10.5). The KEY
        # is request_id; the comparison VALUE is the canonical normalized
        # identity (which excludes request_id/runtime_build_id/
        # authority_instance_id — the Layer-4 rule retained by design).
        table_decision = self._table.classify(request_id, _identity)
        if table_decision.kind == "violation":
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "request_id_reused",
                    "this request_id was already used for a DIFFERENT canonical "
                    "request; a reused id must carry the identical request "
                    "(protocol violation, no execution)",
                ),
                is_response=True,
            )
        if table_decision.kind == "retained":
            # M7-T23: the same completed request returns the RETAINED
            # frame — byte-for-byte, with no second Dispatcher invocation.
            assert table_decision.response is not None
            return table_decision.response
        if table_decision.kind == "full":
            # M7-T57: in-flight entries are pinned and non-evictable, so
            # saturation backpressures NEW admission instead of evicting
            # live owner work.
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "table_full",
                    "the retained request table is fully occupied by in-flight "
                    "owner work; retry after live requests complete",
                ),
                is_response=True,
            )
        if table_decision.kind == "join":
            # M7-T22/T43: the duplicate joins the SAME owner-side work.
            assert table_decision.future is not None
            try:
                return await asyncio.shield(table_decision.future)
            except Exception as exc:  # noqa: BLE001 - the joiner reports, never executes
                return encode_json_frame(
                    IPCRequestOutcome.error(
                        "internal",
                        f"the joined owner-side request failed: {exc!r}",
                    ),
                    is_response=True,
                )
        # kind == "new" (F-74): the TABLE owns the execution task. The
        # wrapper self-completes/abandons the entry at the task's own
        # terminal boundary, so the admitted work survives the caller's
        # disappearance (disconnect, RPC timeout) and duplicates join the
        # SAME pinned task. The caller only ever awaits a shield.
        request_id_str = request_id

        async def _owner_task() -> bytes:
            try:
                frame = await self._admitted_request_pipeline(
                    expected_instance=expected_instance,
                    operation=operation,
                    normalized=normalized,
                )
            except BaseException as exc:
                self._table.abandon(request_id_str, exc)
                raise
            self._table.complete(request_id_str, frame)
            return frame

        task = asyncio.ensure_future(_owner_task())
        self._table.pin_task(request_id_str, task)  # the strong owner-side reference (§10.5)
        return await asyncio.shield(task)

    async def _admitted_request_pipeline(
        self,
        *,
        expected_instance: str,
        operation: str,
        normalized: dict[str, Any],
    ) -> bytes:
        """The drain/admission/Dispatcher tail for a NEW retained request."""
        if self._draining:
            return encode_json_frame(
                IPCRequestOutcome.error("draining", IPC_DRAINING_MESSAGE),
                is_response=True,
            )
        try:
            # THE gate: stale instances lose atomically here, before any
            # Dispatcher work, browser navigation, or journal mutation.
            with self._session.admit(expected_instance_id=expected_instance):
                self._active_requests += 1
                try:
                    # Admitted: the owner task runs to completion even if
                    # the client disconnects (the conservative disconnect
                    # law — response is lost, work is not cancelled).
                    # F-73: reconciliation operations route through the
                    # owner-side operator sessions; everything else is a
                    # capability invocation.
                    if operation in IPC_RECONCILIATION_OPERATIONS:
                        return await self._reconciliation_route(operation, normalized)
                    result = await self._invoke(operation, normalized)
                finally:
                    self._active_requests -= 1
        except AuthorityStaleInstanceError:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "stale_authority_instance",
                    "the owner instance this request addressed is no longer the active owner",
                ),
                is_response=True,
            )
        except AuthorityAdmissionClosedError as exc:
            return encode_json_frame(IPCRequestOutcome.error("draining", str(exc)), is_response=True)

        # The ActionResult -> bounded wire payload (no raw objects escape).
        data: Any
        if hasattr(result, "ok") and result.ok:
            data = result.data if isinstance(result.data, dict) else {}
        else:
            error_msg = getattr(getattr(result, "error", None), "message", "")
            # F-72: a FAILED outcome carries its bounded safety projection —
            # public_side_effect / reconciliation_required / m5_effect_state
            # and friends must survive the boundary so a client can tell an
            # uncertain external mutation from a clean denial.
            return encode_json_frame(
                IPCRequestOutcome.failure(
                    "capability",
                    error_msg or "capability failed",
                    _safety_projection(result),
                ),
                is_response=True,
            )
        try:
            return encode_json_frame(IPCRequestOutcome.ok(data), is_response=True)
        except IPCProtocolError:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "response_oversized",
                    "the response exceeded the wire ceiling",
                ),
                is_response=True,
            )

    # -- F-73: the reconciliation operator wire route (§14.3) --------------

    async def _reconciliation_route(self, operation: str, payload: dict[str, Any]) -> bytes:
        """Owner-side reconciliation over IPC: opaque logical sessions,
        read-only target data, frozen proposals, exact same-session text
        confirmation, and terminal resolution through the owner's stored
        authority. The ReconciliationAuthority (and every coordinator/
        delegate object) never crosses the wire."""
        from webwire.safety.reconciliation_ledger import ReconciliationVerdict
        from webwire.safety.reconciliation_operator import ReconciliationOperatorError

        def _error(code: str, message: str) -> bytes:
            return encode_json_frame(IPCRequestOutcome.error(code, message), is_response=True)

        def _ok(data: Any) -> bytes:
            return encode_json_frame(IPCRequestOutcome.ok(data), is_response=True)

        try:
            if operation == "reconciliation_open":
                if self._reconciliation_provider is None:
                    return _error(
                        "reconciliation_unavailable",
                        "this owner exposes no reconciliation provider",
                    )
                if len(self._reconciliation_sessions) >= IPC_MAX_RECONCILIATION_SESSIONS:
                    return _error(
                        "reconciliation_busy",
                        "the bounded operator-session registry is full",
                    )
                operator_session = self._reconciliation_provider(payload["operator_id"])
                wire_session = _ReconciliationWireSession(
                    instance_id=self._session.authority_instance_id,
                    operator_session=operator_session,
                )
                self._reconciliation_sessions[wire_session.id] = wire_session
                return _ok({"reconciliation_session_id": wire_session.id})

            session_id = payload.get("reconciliation_session_id")
            current: Optional[_ReconciliationWireSession] = (
                self._reconciliation_sessions.get(session_id) if isinstance(session_id, str) else None
            )
            if current is None or current.instance_id != self._session.authority_instance_id:
                return _error(
                    "reconciliation_session_unknown",
                    "unknown, drained, or restarted reconciliation session",
                )
            op = current.operator_session

            if operation == "reconciliation_list":
                targets = op.list_targets()
                return _ok({"targets": [_serialize_target(t) for t in targets]})

            if operation == "reconciliation_show":
                target = op.show_target(payload["effect_id"])
                return _ok({"target": _serialize_target(target)})

            if operation == "reconciliation_prepare":
                verdict = ReconciliationVerdict(payload["verdict"])
                proposal = op.prepare_resolution(
                    effect_id=payload["effect_id"],
                    verdict=verdict,
                    evidence=payload["evidence"],
                    evidence_summary=payload["evidence_summary"],
                )
                serialized = _serialize_proposal(proposal)
                current.proposals[proposal.proposal_id] = serialized
                return _ok({"proposal": serialized})

            if operation == "reconciliation_confirm":
                proposal_id = payload["proposal_id"]
                if proposal_id not in current.proposals:
                    return _error("reconciliation", f"unknown proposal {proposal_id!r} in this session")
                # Exact same-session text confirmation; the minted
                # authority object stays OWNER-PRIVATE.
                authority = op.confirm_resolution(
                    proposal_id,
                    confirmation_text=payload["confirmation_text"],
                )
                current.authorities[proposal_id] = authority
                return _ok({"confirmed": True, "proposal_id": proposal_id})

            if operation == "reconciliation_resolve":
                proposal_id = payload["proposal_id"]
                authority = current.authorities.get(proposal_id)
                if authority is None:
                    return _error(
                        "reconciliation",
                        f"proposal {proposal_id!r} has no confirmed authority in this session",
                    )
                resolution = op.resolve(proposal_id, authority=authority)
                return _ok({"resolution": _serialize_resolution(resolution)})

            return _error("unsupported_operation", f"unknown reconciliation operation {operation!r}")
        except ReconciliationOperatorError as exc:
            return _error("reconciliation", f"{getattr(exc, 'reason', None) or exc}")
        except (KeyError, ValueError, TypeError) as exc:
            return _error("reconciliation", f"malformed reconciliation request: {exc!r}")


# -- F-73: the reconciliation operator wire route (frozen §14.3) ------------

# The six reconciliation operations. They are routed INSIDE the same
# admission/stale/table gates as capabilities; the owner-side registry
# holds the logical sessions.
IPC_RECONCILIATION_OPERATIONS = frozenset(
    {
        "reconciliation_open",
        "reconciliation_list",
        "reconciliation_show",
        "reconciliation_prepare",
        "reconciliation_confirm",
        "reconciliation_resolve",
    }
)


def _serialize_target(target: Any) -> dict[str, Any]:
    first = target.first_record
    state = getattr(first, "state", None)
    return {
        "effect_id": target.effect_id,
        "semantic_key": getattr(first, "semantic_key", None),
        "action_type": getattr(first, "action_type", None),
        "intent_hash": getattr(first, "intent_hash", None),
        "actor_id": getattr(first, "actor_id", None),
        "target_type": getattr(first, "target_type", None),
        "target_id": getattr(first, "target_id", None),
        "state": getattr(state, "value", state),
        "timestamp": getattr(first, "timestamp", None),
    }


def _serialize_proposal(proposal: Any) -> dict[str, Any]:
    verdict = getattr(proposal, "verdict", None)
    return {
        "proposal_id": proposal.proposal_id,
        "effect_id": proposal.effect_id,
        "semantic_key": proposal.semantic_key,
        "action_type": proposal.action_type,
        "intent_hash": proposal.intent_hash,
        "actor_id": proposal.actor_id,
        "target_type": proposal.target_type,
        "target_id": proposal.target_id,
        "verdict": getattr(verdict, "value", verdict),
        "operator_id": proposal.operator_id,
        "evidence_hash": proposal.evidence_hash,
        "evidence_summary": proposal.evidence_summary,
        "confirmation_text": proposal.confirmation_text,
    }


def _serialize_resolution(resolution: Any) -> dict[str, Any]:
    record = getattr(resolution, "record", None)
    status = getattr(resolution, "recovery_status", None)
    record_data = _json_safe(vars(record)) if hasattr(record, "__dict__") else repr(record)[:512]
    return {
        "confirmation_epoch": resolution.confirmation_epoch,
        "record": record_data,
        "recovery_status": {
            "hydrated": getattr(status, "hydrated", None),
            "available": getattr(status, "available", None),
            "unresolved_semantic_keys": list(getattr(status, "unresolved_semantic_keys", ()) or ()),
            "unresolved_effect_count": getattr(status, "unresolved_effect_count", None),
        },
    }
