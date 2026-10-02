"""M7 Layer 4 — the authority IPC server: the owner-side security boundary.

The processing pipeline (frozen contract; the Dispatcher is reachable ONLY
after every earlier gate passes):

    1. frame decode (bounded, non-executable, strict UTF-8 JSON)
    2. handshake compatibility (exact protocol + exact runtime_build_id)
    3. operation allowlist (exactly five names; whoami/writes/media/
       download rejected before Dispatcher invocation)
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
from typing import Any, Optional

from webwire.authority_ipc_endpoint import IPCEndpoint, create_endpoint
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
from webwire.authority_session import (
    AuthorityAdmissionClosedError,
    AuthoritySession,
    AuthorityStaleInstanceError,
)

logger = logging.getLogger(__name__)

__all__ = ["AuthorityIPCServer", "IPC_SERVER_MAX_CONCURRENT", "IPC_DRAINING_MESSAGE"]


IPC_SERVER_MAX_CONCURRENT = 4  # bounded connection concurrency
IPC_DRAINING_MESSAGE = "authority session is draining: new IPC work refused"


class IPCRequestOutcome:
    """The bounded result envelope sent back over the wire."""

    @staticmethod
    def ok(data: Any) -> dict[str, Any]:
        return {"ok": True, "data": data}

    @staticmethod
    def error(code: str, message: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": code, "message": message}}


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
        max_concurrent: int = IPC_SERVER_MAX_CONCURRENT,
    ) -> None:
        if runtime_build_id is None:
            runtime_build_id = compute_runtime_build_id()
        self._session = session
        self._authority_domain = authority_domain
        self._invoke = invoke
        self._runtime_build_id = runtime_build_id
        self._max_concurrent = max_concurrent
        self._active_requests = 0
        self._draining = False
        self._endpoint: Optional[IPCEndpoint] = None
        self._listener_task: Optional[asyncio.Task] = None

    @property
    def endpoint_path(self) -> Optional[str]:
        return self._endpoint.path if self._endpoint else None

    @property
    def active_requests(self) -> int:
        return self._active_requests

    # -- lifecycle (called from Dispatcher.start/stop) ----------------------

    def start(self) -> None:
        """Bind the secured IPC endpoint (before READY). Raises on any
        bind/security failure — the Dispatcher treats that as a startup
        failure and follows the fail-closed teardown law."""
        self._endpoint = create_endpoint(self._authority_domain)
        self._endpoint.bind()
        self._draining = False

    def begin_drain(self) -> None:
        """Stop accepting new IPC work (called at DRAINING). Already-open
        connections cannot submit fresh work after this point."""
        self._draining = True

    def stop(self) -> None:
        """Close/unlink the endpoint (called BEFORE terminalize/release)."""
        self._draining = True
        if self._endpoint is not None:
            try:
                self._endpoint.close()
            finally:
                self._endpoint = None

    # -- request processing (the security pipeline) -------------------------

    def _hello(self) -> AuthorityHello:
        return AuthorityHello(
            protocol_version=IPC_PROTOCOL_VERSION,
            runtime_version=sys.version.split()[0],
            runtime_build_id=self._runtime_build_id,
            authority_instance_id=self._session.authority_instance_id,
            authority_domain=str(self._authority_domain),
            supported_operations=IPC_SUPPORTED_OPERATIONS,
            lifecycle_state=self._session.state.value,
        )

    async def process_request(self, raw_header: FrameHeader, raw_payload: bytes) -> bytes:
        """The full security pipeline for one framed request.

        Returns the framed response. The Dispatcher is reachable only
        after: frame decode → handshake fields → operation allowlist →
        schema validation → stale-instance admission."""
        from webwire.authority_ipc_framing import decode_json_frame

        try:
            request = decode_json_frame(raw_header, raw_payload)
        except IPCProtocolError as exc:
            return encode_json_frame(IPCRequestOutcome.error("protocol", str(exc)), is_response=True)

        # Handshake/compatibility fields on the request envelope.
        protocol_version = request.get("protocol_version")
        if protocol_version != IPC_PROTOCOL_VERSION:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "protocol_mismatch",
                    f"protocol version {protocol_version!r} is not {IPC_PROTOCOL_VERSION}",
                ),
                is_response=True,
            )
        client_build_id = request.get("runtime_build_id")
        if client_build_id is not None and client_build_id != self._runtime_build_id:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "build_mismatch",
                    "runtime build mismatch: the owner and client are different artifacts",
                ),
                is_response=True,
            )

        operation = request.get("operation")
        if not isinstance(operation, str) or operation not in IPC_SUPPORTED_OPERATIONS:
            # The allowlist: whoami, writes, media, download, reconciliation
            # — everything outside the five names — dies HERE, before the
            # Dispatcher is reachable.
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

        # Stale-instance admission: the Layer-3 primitive, not a pre-check.
        expected_instance = request.get("authority_instance_id")
        if not isinstance(expected_instance, str) or not expected_instance:
            return encode_json_frame(
                IPCRequestOutcome.error("missing_instance", "authority_instance_id is required"),
                is_response=True,
            )
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
                    result = await self._invoke(operation, normalized)
                finally:
                    self._active_requests -= 1
        except AuthorityStaleInstanceError:
            return encode_json_frame(
                IPCRequestOutcome.error(
                    "stale_instance",
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
            return encode_json_frame(
                IPCRequestOutcome.error("capability", error_msg or "read failed"),
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
