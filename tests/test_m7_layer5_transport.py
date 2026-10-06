"""M7 Layer 5 transport tests — confirmation + retained requests over REAL
sockets (frozen §13, §10.5, §16; M7-T27/T28/T29/T30/T43).

The confirmation half wires a REAL ConfirmationState behind the REAL
transport: clients are plain IPCClient connections, tokens are minted and
consumed by the owner's single canonical state, and the multi-client /
epoch / stale-instance laws are exercised end to end on the wire.
"""

from __future__ import annotations

import asyncio
import secrets
import socket
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import encode_json_frame
from webwire.authority_ipc_protocol import IPC_PROTOCOL_VERSION, compute_runtime_build_id
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result
from webwire.safety.confirmation_state import ConfirmationState
from webwire.safety.models import RiskTier

_TIER = RiskTier.PUBLIC_CONTENT_IRREVERSIBLE  # post_text's tier

pytestmark = pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="AF_UNIX sockets are POSIX-only")

BUILD_ID = compute_runtime_build_id()
INTENT = "layer5-wire-intent"
CAP = "post_text"


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("layer5-transport"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _confirmation_owner(tmp_path: Path, *, confirmation: ConfirmationState, log: list):
    """A pipeline whose invoke uses the REAL ConfirmationState: no token →
    preview + mint; token → validate_and_consume → execute."""
    session = _session_ready()
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()

    async def invoke(name: str, input: dict) -> Any:
        log.append(dict(input))
        token_str = input.get("confirmation_token")
        if token_str is None:
            token = confirmation.issue(intent_hash=INTENT, risk_tier=_TIER, capability_name=CAP)
            return ok_result(data={"preview": "Will post", "confirmation_token": token.token})
        _token, reason = confirmation.validate_and_consume(
            token_str, intent_hash=INTENT, risk_tier=_TIER, capability_name=CAP
        )
        if reason is not None:
            return ok_result(data={"confirmation_rejected": reason})
        return ok_result(data={"executed": True})

    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=tmp_path,
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=tmp_path, loop=loop)
    transport.start()
    return session, ipc, transport, loop, str(transport.endpoint_path or "")


def _wire_request(path: str, instance_id: str, operation: str, payload: dict) -> dict:
    """One request on ONE fresh connection (the frozen wire law: connect →
    hello → one framed request → response → close). The confirmation flow
    links requests by TOKEN, never by connection lifetime."""
    client = IPCClient(path, expected_build_id=BUILD_ID)
    client.connect()
    try:
        return client.request(_envelope(operation, payload, instance_id=instance_id))
    finally:
        client.close()


def _teardown(transport, loop) -> None:
    transport.stop()
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.2)
    loop.close()


def _envelope(operation: str, payload: Any, *, instance_id: str, request_id: str | None = None):
    return {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "request_id": request_id or secrets.token_hex(16),
        "authority_instance_id": instance_id,
        "runtime_build_id": BUILD_ID,
    }


async def test_two_clients_share_one_owner_confirmation_state(tmp_path: Path) -> None:
    """M7-T27: client A previews (token T), client B previews (token U) —
    both live in the SAME owner ConfirmationState and both consume
    successfully against it."""
    confirmation = ConfirmationState(ttl_seconds=300)
    log: list = []
    session, ipc, transport, loop, path = _confirmation_owner(tmp_path, confirmation=confirmation, log=log)
    try:
        instance = session.authority_instance_id
        hello_a = IPCClient(path, expected_build_id=BUILD_ID).connect()
        hello_b = IPCClient(path, expected_build_id=BUILD_ID).connect()
        assert hello_a.authority_instance_id == hello_b.authority_instance_id

        # Each request rides its OWN connection (one request per
        # connection); the two clients share the owner's token state.
        preview_a = _wire_request(path, instance, "post_text", {"text": "from A"})
        preview_b = _wire_request(path, instance, "post_text", {"text": "from B"})
        token_a = preview_a["data"]["confirmation_token"]
        token_b = preview_b["data"]["confirmation_token"]
        assert token_a and token_b and token_a != token_b

        executed_a = _wire_request(
            path, instance, "post_text", {"text": "from A", "confirmation_token": token_a}
        )
        executed_b = _wire_request(
            path, instance, "post_text", {"text": "from B", "confirmation_token": token_b}
        )
        assert executed_a["data"]["executed"] is True
        assert executed_b["data"]["executed"] is True
    finally:
        _teardown(transport, loop)


async def test_same_token_race_allows_at_most_one_success(tmp_path: Path) -> None:
    """M7-T28: two clients race the SAME confirmation token — the
    existing single-use consume boundary permits at most one normal
    success path; the loser observes consumed_token."""
    confirmation = ConfirmationState(ttl_seconds=300)
    log: list = []
    session, ipc, transport, loop, path = _confirmation_owner(tmp_path, confirmation=confirmation, log=log)
    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        preview = client.request(
            _envelope("post_text", {"text": "raced"}, instance_id=session.authority_instance_id)
        )
        token = preview["data"]["confirmation_token"]

        results: list[dict] = []

        def _consume() -> None:
            c = IPCClient(path, expected_build_id=BUILD_ID)
            c.connect()
            results.append(
                c.request(
                    _envelope(
                        "post_text",
                        {"text": "raced", "confirmation_token": token},
                        instance_id=session.authority_instance_id,
                    )
                )
            )
            c.close()

        first = threading.Thread(target=_consume)
        second = threading.Thread(target=_consume)
        first.start()
        second.start()
        first.join(timeout=15)
        second.join(timeout=15)

        executed = [r for r in results if r["data"].get("executed") is True]
        rejected = [r for r in results if r["data"].get("confirmation_rejected") == "consumed_token"]
        assert len(executed) == 1, "exactly one success path"
        assert len(rejected) == 1, "the loser observes single-use consumption"
        client.close()
    finally:
        _teardown(transport, loop)


async def test_reconciliation_epoch_advances_stales_all_client_tokens(tmp_path: Path) -> None:
    """M7-T29: M6 reconciliation advances ONE shared epoch — pending
    tokens from every client go stale under the same owner state."""
    confirmation = ConfirmationState(ttl_seconds=300)
    log: list = []
    session, ipc, transport, loop, path = _confirmation_owner(tmp_path, confirmation=confirmation, log=log)
    try:
        instance = session.authority_instance_id
        token_a = _wire_request(path, instance, "post_text", {"text": "A"})["data"]["confirmation_token"]
        token_b = _wire_request(path, instance, "post_text", {"text": "B"})["data"]["confirmation_token"]

        # The owner-side reconciliation event advances the shared epoch.
        confirmation.advance_epoch()

        for text, token in (("A", token_a), ("B", token_b)):
            response = _wire_request(
                path, instance, "post_text", {"text": text, "confirmation_token": token}
            )
            assert response["data"].get("confirmation_rejected") == "stale_confirmation_epoch"
    finally:
        _teardown(transport, loop)


async def test_stale_owner_instance_token_cannot_reach_execution(tmp_path: Path) -> None:
    """M7-T30 (wire half): a token held against a PREVIOUS owner instance
    is rejected as stale_authority_instance BEFORE execution — the old
    token never reaches current protocol execution."""
    confirmation = ConfirmationState(ttl_seconds=300)
    log: list = []
    session, ipc, transport, loop, path = _confirmation_owner(tmp_path, confirmation=confirmation, log=log)
    try:
        client = IPCClient(path, expected_build_id=BUILD_ID)
        client.connect()
        old_token = client.request(
            _envelope("post_text", {"text": "old"}, instance_id=session.authority_instance_id)
        )["data"]["confirmation_token"]
        client.close()

        # The client replays the old token against a STALE instance id.
        stale_client = IPCClient(path, expected_build_id=BUILD_ID)
        stale_client.connect()
        response = stale_client.request(
            _envelope(
                "post_text",
                {"text": "old", "confirmation_token": old_token},
                instance_id="f" * 64,  # the dead owner's instance id
            )
        )
        assert response["ok"] is False
        assert response["error"]["code"] == "stale_authority_instance"
        stale_client.close()
        # No consume happened: the token is still pending authority.
        _token, reason = confirmation.validate_and_consume(
            old_token, intent_hash=INTENT, risk_tier=_TIER, capability_name=CAP
        )
        assert reason is None, "the stale replay must not consume the token"
    finally:
        _teardown(transport, loop)


async def test_retry_after_disconnect_joins_same_owner_request(tmp_path: Path) -> None:
    """M7-T42/T43: a mutating request is admitted, the client loses the
    connection, a retry with the SAME request id + canonical request
    JOINS the same owner-side work — one execution, both observe it."""
    release = asyncio.Event()
    executions: list[dict] = []
    session = _session_ready()
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()

    async def invoke(name: str, input: dict) -> Any:
        executions.append(dict(input))
        await release.wait()
        return ok_result(data={"posted": True})

    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=tmp_path,
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=tmp_path, loop=loop)
    transport.start()
    path = str(transport.endpoint_path or "")
    rid = secrets.token_hex(16)
    envelope = _envelope(
        "post_text", {"text": "uncertain"}, instance_id=session.authority_instance_id, request_id=rid
    )

    try:
        # First client sends the mutation and forcibly disconnects.
        lost = IPCClient(path, expected_build_id=BUILD_ID)
        lost.connect()

        def _send_and_vanish() -> None:
            lost._sock.send(encode_json_frame(envelope))
            lost._sock.close()

        sender = threading.Thread(target=_send_and_vanish)
        sender.start()
        sender.join(timeout=5)

        # The admitted work is in flight; wait for it to be pinned
        # (polling a thread-mutated list).
        deadline = time.monotonic() + 5
        while not executions and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert len(executions) == 1, "the mutation was admitted despite the disconnect"

        # The retry joins the same owner-side request.
        retry = IPCClient(path, expected_build_id=BUILD_ID)
        retry.connect()
        responses: list[dict] = []

        def _retry() -> None:
            responses.append(retry.request(envelope))

        retryer = threading.Thread(target=_retry)
        retryer.start()
        await asyncio.sleep(0.1)
        loop.call_soon_threadsafe(release.set)
        retryer.join(timeout=15)

        assert responses and responses[0]["ok"] is True
        assert responses[0]["data"]["posted"] is True
        assert len(executions) == 1, "the retry JOINED; no second execution"
        retry.close()
    finally:
        _teardown(transport, loop)
