"""M7 Layer 5 — the extended IPC surface, tests-first (frozen §10.3/§10.5,
§13, §14, M7-RV10).

Layer 5 adds to the wire: the authority-establishing `whoami` read, the
six non-file-backed writes (post_text, reply_post, quote_post,
delete_post, bookmark_post, like_post) with the existing
preview/token/confirm flow routed through the owner, and the retained
request table enforced inside the security pipeline. Media-backed
writes, artifacts, and download_image remain unadvertised (Layer 6
forbidden scope — M7-T66).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import encode_json_frame, parse_frame_header
from webwire.authority_ipc_protocol import (
    IPC_PROTOCOL_VERSION,
    IPC_SUPPORTED_OPERATIONS,
    compute_runtime_build_id,
)
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()

LAYER5_READS = frozenset({"whoami"})
LAYER5_WRITES = frozenset(
    {"post_text", "reply_post", "quote_post", "delete_post", "bookmark_post", "like_post"}
)
LAYER6_FORBIDDEN = frozenset(
    {
        "post_photo",
        "reply_photo",
        "quote_photo",
        "post_multi_image",
        "reply_multi_image",
        "quote_multi_image",
        "download_image",
        "compose_post",
    }
)


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("layer5-test"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _server(
    session: AuthoritySession,
    invoke_log: list | None = None,
    invoke_result: Any = None,
) -> AuthorityIPCServer:
    async def invoke(name: str, input: dict) -> Any:
        if invoke_log is not None:
            invoke_log.append((name, dict(input)))
        return invoke_result or ok_result(data={"ok": True})

    return AuthorityIPCServer(
        session=session,
        authority_domain=Path("layer5-test"),
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )


def _frame(operation: str, payload: Any, *, instance_id: str, request_id: str | None = None) -> bytes:
    body: dict[str, Any] = {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "request_id": request_id or secrets.token_hex(16),
        "authority_instance_id": instance_id,
        "runtime_build_id": BUILD_ID,
    }
    return encode_json_frame(body)


async def _respond(server: AuthorityIPCServer, frame: bytes) -> dict[str, Any]:
    header, consumed = parse_frame_header(frame)
    response = await server.process_request(header, frame[consumed:])
    return json.loads(response[8:])


# ---------------------------------------------------------------------------
# The extended advertised surface (frozen §10.3 Layer-5 delta)
# ---------------------------------------------------------------------------


def test_layer5_operations_are_advertised() -> None:
    """whoami + the six non-file writes join the advertised allowlist."""
    assert LAYER5_READS <= IPC_SUPPORTED_OPERATIONS
    assert LAYER5_WRITES <= IPC_SUPPORTED_OPERATIONS
    assert {"health", "read", "read_profile", "read_thread", "read_search"} <= IPC_SUPPORTED_OPERATIONS


def test_layer6_media_scope_remains_unadvertised() -> None:
    """M7-T66: photo/multi-image/media writes, artifacts, and the local
    output capability are NOT advertised before the Layer-6 media-ingress
    qualification — requesting them dies at the allowlist, before the
    Dispatcher is reachable (no owner path resolution, preview, token,
    or effect authority)."""
    assert not (LAYER6_FORBIDDEN & IPC_SUPPORTED_OPERATIONS)
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)

    async def _check() -> dict[str, Any]:
        header, payload = parse_and_split(
            _frame(
                "post_photo",
                {"text": "x", "image_path": "C:/raw/client/path.png"},
                instance_id=session.authority_instance_id,
            )
        )
        response = await server.process_request(header, payload)
        return json.loads(response[8:])

    import asyncio

    response = asyncio.run(_check())
    assert response["ok"] is False
    assert response["error"]["code"] == "unsupported_operation"
    assert log == [], "no Dispatcher invocation for forbidden media scope"


def parse_and_split(frame: bytes):
    header, consumed = parse_frame_header(frame)
    return header, frame[consumed:]


async def test_whoami_routes_through_admission() -> None:
    """M7-RV10 qualification: whoami crosses IPC as an
    authority-establishing read — routed through the same gates as every
    other operation (exact build, admission, stale instance)."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log, invoke_result=ok_result(data={"handle": "@owner"}))
    response = await _respond(server, _frame("whoami", {}, instance_id=session.authority_instance_id))
    assert response["ok"] is True
    assert response["data"]["handle"] == "@owner"
    assert log == [("whoami", {})]


async def test_whoami_payload_must_be_empty() -> None:
    session = _session_ready()
    server = _server(session)
    response = await _respond(
        server, _frame("whoami", {"unexpected": 1}, instance_id=session.authority_instance_id)
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "schema"


# ---------------------------------------------------------------------------
# Write schemas: strict, with the opaque confirmation token field
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("post_text", {"text": "hello world"}),
        ("reply_post", {"post_url": "https://x.com/a/1", "text": "reply"}),
        ("reply_post", {"target_post_id": "1", "text": "reply"}),
        ("quote_post", {"post_url": "https://x.com/a/1", "text": "quote"}),
        ("delete_post", {"post_url": "https://x.com/a/1"}),
        ("delete_post", {"target_post_id": "1"}),
        ("bookmark_post", {"post_id": "1"}),
        ("bookmark_post", {"post_url": "https://x.com/a/1"}),
        ("like_post", {"post_url": "https://x.com/a/1"}),
    ],
)
async def test_write_schemas_accept_canonical_shapes(operation: str, payload: dict) -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    response = await _respond(server, _frame(operation, payload, instance_id=session.authority_instance_id))
    assert response["ok"] is True, response
    assert log == [(operation, payload)]


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("post_text", {}),  # missing text
        ("post_text", {"text": 123}),  # non-string text
        ("post_text", {"text": "x", "image_path": "C:/raw.png"}),  # unknown key (raw path!)
        ("reply_post", {"text": "no target"}),
        ("reply_post", {"post_url": "https://x.com/a/1"}),  # missing text
        ("quote_post", {"target_post_id": 5, "text": "t"}),  # non-string id
        ("delete_post", {}),  # no target at all
        ("bookmark_post", {}),  # no target
        ("like_post", {"post_id": None}),  # null is not a target
        ("post_text", {"text": "x", "confirmation_token": 99}),  # non-string token
        ("post_text", {"text": "x", "confirmation_token": "short"}),  # not opaque entropy
    ],
)
async def test_write_schemas_reject_malformed(operation: str, payload: dict) -> None:
    """Malformed write payloads die at schema, before admission or any
    Dispatcher reachability — including raw client filesystem paths,
    which never reach capability compose (M7-T67's spirit at Layer 5)."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    response = await _respond(server, _frame(operation, payload, instance_id=session.authority_instance_id))
    assert response["ok"] is False
    assert response["error"]["code"] == "schema"
    assert log == []


async def test_confirmation_token_round_trips_into_the_invocation() -> None:
    """The confirm call carries the opaque token through schema into the
    owner invocation — the existing WriteKernel consume boundary stays
    the authority; the pipeline only routes."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    token = secrets.token_hex(32)
    response = await _respond(
        server,
        _frame(
            "post_text",
            {"text": "hello", "confirmation_token": token},
            instance_id=session.authority_instance_id,
        ),
    )
    assert response["ok"] is True
    assert log == [("post_text", {"text": "hello", "confirmation_token": token})]


# ---------------------------------------------------------------------------
# Retained request table inside the pipeline (M7-T22/T23/T24 via IPC)
# ---------------------------------------------------------------------------


async def test_pipeline_retained_response_for_same_completed_request() -> None:
    """M7-T23 through process_request: the same request_id + same
    canonical request returns the RETAINED frame with NO second
    Dispatcher invocation."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log, invoke_result=ok_result(data={"posts": [{"id": "1"}]}))
    rid = secrets.token_hex(16)
    frame = _frame("read", {"post_url": "u"}, instance_id=session.authority_instance_id, request_id=rid)

    first = await _respond(server, frame)
    assert first["ok"] is True
    second = await _respond(server, frame)
    assert second == first
    assert len(log) == 1, "retained response must not re-invoke the Dispatcher"


async def test_pipeline_reused_id_different_request_is_violation() -> None:
    """M7-T24 through process_request: same id, different canonical
    request → protocol violation, no execution."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    rid = secrets.token_hex(16)
    instance = session.authority_instance_id

    first = await _respond(
        server, _frame("read", {"post_url": "u"}, instance_id=instance, request_id=rid)
    )
    assert first["ok"] is True
    second = await _respond(
        server, _frame("read", {"post_url": "DIFFERENT"}, instance_id=instance, request_id=rid)
    )
    assert second["ok"] is False
    assert second["error"]["code"] == "request_id_reused"
    assert len(log) == 1


async def test_pipeline_concurrent_duplicate_joins_inflight_work() -> None:
    """M7-T22 through process_request: two concurrently in-flight frames
    with the same id + canonical request produce ONE invocation and both
    callers observe the same response."""
    session = _session_ready()
    log: list = []

    release = __import__("asyncio").Event()

    async def invoke(name: str, input: dict) -> Any:
        log.append((name, dict(input)))
        await release.wait()
        return ok_result(data={"joined": True})

    server = AuthorityIPCServer(
        session=session,
        authority_domain=Path("layer5-test"),
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )
    rid = secrets.token_hex(16)
    frame = _frame("read", {"post_url": "u"}, instance_id=session.authority_instance_id, request_id=rid)

    import asyncio

    first_task = asyncio.create_task(_respond(server, frame))
    second_task = asyncio.create_task(_respond(server, frame))
    await __import__("asyncio").sleep(0.05)  # let both enter the table
    release.set()
    first, second = await asyncio.gather(first_task, second_task)
    assert first["ok"] is True and second["ok"] is True
    assert first == second
    assert len(log) == 1, "the duplicate must join, not re-execute"


# ---------------------------------------------------------------------------
# Restart fencing + confirmation authority (mandatory regressions)
# ---------------------------------------------------------------------------


async def test_stale_instance_cannot_mint_or_consume_confirmation() -> None:
    """Mandatory regression: a stale owner instance is rejected BEFORE
    execution — an old token can never reach current protocol execution
    (§13) and a stale client cannot mint a new token through this owner."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    stale = "f" * 64
    token = secrets.token_hex(32)
    response = await _respond(
        server,
        _frame(
            "post_text",
            {"text": "x", "confirmation_token": token},
            instance_id=stale,
        ),
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "stale_authority_instance"
    assert log == []


async def test_new_owner_table_is_empty_old_ids_execute_fresh() -> None:
    """M7-T26 (pipeline half): the retained table is process-local — a
    successor owner (new AuthorityIPCServer instance) starts with an
    empty table, so an old id is simply NEW work again under the new
    instance's admission."""
    first_session = _session_ready()
    log: list = []
    first = _server(first_session, invoke_log=log)
    rid = secrets.token_hex(16)
    frame1 = _frame(
        "read", {"post_url": "u"}, instance_id=first_session.authority_instance_id, request_id=rid
    )
    assert (await _respond(first, frame1))["ok"] is True

    successor_session = _session_ready()
    successor = _server(successor_session, invoke_log=log)
    frame2 = _frame(
        "read", {"post_url": "u"}, instance_id=successor_session.authority_instance_id, request_id=rid
    )
    assert (await _respond(successor, frame2))["ok"] is True
    assert len(log) == 2, "each owner executes its own work"
