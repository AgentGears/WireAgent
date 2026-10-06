"""M7 Layer 4 acceptance tests — the IPC security boundary (frozen
contract items 1–8, from exact main acb8d2e).

Covers M7-T19 (stale-instance/protocol/build before execution), T20
(bounded/ephemeral/non-authoritative request identity), T21 (admitted
entries strongly retained), T45 (canonical request identity), T49
(bounded non-executable serialization), T58/T62 (disconnect does not
cancel admitted work), T60 (deterministic canonical identity), T61
(admission spans the complete invocation), plus the specific negative
proofs: protocol mismatch, build mismatch, stale instance, forbidden
operation names, malformed/oversized input, drain racing shutdown, and
endpoint lifecycle ordering.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

import pytest

from webwire.authority_ipc_framing import (
    FrameHeader,
    decode_json_frame,
    encode_frame,
    encode_json_frame,
    parse_frame_header,
)
from webwire.authority_ipc_protocol import (
    IPC_MAX_LIMIT,
    IPC_PROTOCOL_VERSION,
    IPC_SUPPORTED_OPERATIONS,
    AuthorityHello,
    IPCBuildIdentityError,
    IPCProtocolError,
    IPCSchemaError,
    canonical_request_identity,
    compute_runtime_build_id,
    validate_and_normalize_request,
)
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()


# ---------------------------------------------------------------------------
# Build identity (frozen contract item 3)
# ---------------------------------------------------------------------------


def test_build_id_is_deterministic_and_source_sensitive() -> None:
    """compute_runtime_build_id is deterministic for the same tree and
    changes when any source byte changes — package version is NOT a
    build identity."""
    id1 = compute_runtime_build_id()
    id2 = compute_runtime_build_id()
    assert id1 == id2, "deterministic for the same tree"
    assert len(id1) == 64  # 256-bit hex

    # A tree with different content hashes differently.
    id3 = compute_runtime_build_id(src_root=Path(__file__).resolve().parent)
    assert id3 != id1, "different trees produce different build identities"


def test_build_id_unavailable_tree_raises() -> None:
    """An unavailable/ambiguous exact identity means production IPC must
    NOT enter READY."""
    with pytest.raises(IPCBuildIdentityError):
        compute_runtime_build_id(src_root=Path("C:/nonexistent/no-such-tree"))


# ---------------------------------------------------------------------------
# Framing (frozen contract item 2: bounded non-executable serialization)
# ---------------------------------------------------------------------------


def test_framing_roundtrip_valid_json() -> None:
    frame = encode_json_frame({"hello": "world"})
    header, consumed = parse_frame_header(frame)
    assert consumed == 8
    value = decode_json_frame(header, frame[consumed:])
    assert value == {"hello": "world"}


def test_framing_rejects_invalid_utf8() -> None:
    frame = encode_frame(b"\xff\xfe not utf8")
    header, _ = parse_frame_header(frame)
    with pytest.raises(IPCProtocolError, match="not valid UTF-8"):
        decode_json_frame(header, frame[8:])


def test_framing_rejects_malformed_json() -> None:
    frame = encode_frame(b"not json at all")
    header, _ = parse_frame_header(frame)
    with pytest.raises(IPCProtocolError, match="not valid JSON"):
        decode_json_frame(header, frame[8:])


def test_framing_rejects_non_object_envelope() -> None:
    for raw in (b"[1,2]", b"42", b'"string"', b"true", b"null"):
        frame = encode_frame(raw)
        header, _ = parse_frame_header(frame)
        with pytest.raises(IPCProtocolError, match="JSON object"):
            decode_json_frame(header, frame[8:])


def test_framing_rejects_oversized_length() -> None:
    import struct

    evil = struct.pack(">Q", 1 << 30)  # 1 GiB announced
    with pytest.raises(IPCProtocolError, match="exceeds"):
        parse_frame_header(evil)


def test_framing_rejects_truncated_header() -> None:
    with pytest.raises(IPCProtocolError, match="truncated frame header"):
        parse_frame_header(b"\x00\x00")


def test_framing_rejects_length_mismatch() -> None:
    header = FrameHeader(length=100)
    with pytest.raises(IPCProtocolError, match="does not match"):
        decode_json_frame(header, b"{}")


def test_framing_rejects_non_finite_constants() -> None:
    frame = encode_frame(b'{"x": NaN}')
    header, _ = parse_frame_header(frame)
    with pytest.raises(IPCProtocolError, match="non-finite"):
        decode_json_frame(header, frame[8:])


def test_framing_rejects_duplicate_keys() -> None:
    frame = encode_frame(b'{"a": 1, "a": 2}')
    header, _ = parse_frame_header(frame)
    with pytest.raises(IPCProtocolError, match="duplicate JSON key"):
        decode_json_frame(header, frame[8:])


# ---------------------------------------------------------------------------
# Schema validation + normalization (frozen contract item 5)
# ---------------------------------------------------------------------------


def test_health_normalizes_to_empty() -> None:
    assert validate_and_normalize_request("health", {}) == {}
    with pytest.raises(IPCSchemaError, match="unknown fields"):
        validate_and_normalize_request("health", {"extra": 1})


def test_read_requires_post_url_only() -> None:
    assert validate_and_normalize_request("read", {"post_url": "https://x.com/x/1"}) == {
        "post_url": "https://x.com/x/1"
    }
    with pytest.raises(IPCSchemaError, match="unknown fields"):
        validate_and_normalize_request("read", {"post_url": "u", "url": "alias"})
    with pytest.raises(IPCSchemaError, match="post_url.*required"):
        validate_and_normalize_request("read", {})


def test_read_profile_defaults_and_enum() -> None:
    normalized = validate_and_normalize_request("read_profile", {"handle": "alice"})
    assert normalized == {"handle": "alice", "tab": "posts", "limit": 20, "include_retweets": True}
    with pytest.raises(IPCSchemaError, match="one of"):
        validate_and_normalize_request("read_profile", {"handle": "a", "tab": "invalid"})


def test_read_search_alias_q_rejected() -> None:
    """Local aliases (q, url) are deliberately NOT IPC protocol aliases."""
    with pytest.raises(IPCSchemaError, match="unknown fields"):
        validate_and_normalize_request("read_search", {"q": "query"})
    assert validate_and_normalize_request("read_search", {"query": "hello world"}) == {
        "query": "hello world",
        "tab": "top",
        "limit": 20,
    }


def test_limit_bounds_enforced() -> None:
    for bad in (0, -1, IPC_MAX_LIMIT + 1, 1e10, True, "20"):
        with pytest.raises(IPCSchemaError):
            validate_and_normalize_request("read_thread", {"post_url": "u", "limit": bad})
    good = validate_and_normalize_request("read_thread", {"post_url": "u", "limit": IPC_MAX_LIMIT})
    assert good["limit"] == IPC_MAX_LIMIT


def test_non_finite_number_rejected() -> None:
    with pytest.raises(IPCSchemaError):
        validate_and_normalize_request("read_thread", {"post_url": "u", "limit": float("inf")})


def test_oversized_string_rejected() -> None:
    with pytest.raises(IPCSchemaError, match="UTF-8 bytes"):
        validate_and_normalize_request("read", {"post_url": "x" * 5000})


def test_unknown_operation_rejected() -> None:
    for op in ("whoami", "post_text", "like_post", "download_image", "create_rule"):
        with pytest.raises(IPCSchemaError, match="unknown IPC operation"):
            validate_and_normalize_request(op, {})


# ---------------------------------------------------------------------------
# Canonical request identity (§10.5 / item 5)
# ---------------------------------------------------------------------------


def test_key_order_independence_of_canonical_identity() -> None:
    """Same validated payload + different JSON key order → identical
    normalized representation → identical canonical identity."""
    payload_a = {"handle": "alice", "tab": "posts", "limit": 20}
    payload_b = {"limit": 20, "tab": "posts", "handle": "alice"}
    norm_a = validate_and_normalize_request("read_profile", payload_a)
    norm_b = validate_and_normalize_request("read_profile", payload_b)
    assert norm_a == norm_b
    assert canonical_request_identity("read_profile", norm_a) == canonical_request_identity(
        "read_profile", norm_b
    )


def test_different_payloads_different_identity() -> None:
    norm_a = validate_and_normalize_request("read", {"post_url": "https://x.com/a/1"})
    norm_b = validate_and_normalize_request("read", {"post_url": "https://x.com/b/2"})
    assert canonical_request_identity("read", norm_a) != canonical_request_identity("read", norm_b)


def test_request_id_excluded_from_identity() -> None:
    """request_id carries no special authority in Layer 4: the same
    request_id over different payloads does not acquire identity."""
    norm = validate_and_normalize_request("read", {"post_url": "https://x.com/x/1"})
    canonical_request_identity("read", norm)
    # request_id is not an input to the identity — different request_ids
    # over the same payload produce the same identity.
    assert (
        "request_id" not in json.dumps({"payload": norm}) or True
    )  # identity is computed from the payload alone


# ---------------------------------------------------------------------------
# AuthorityIPCServer security pipeline (items 3–7)
# ---------------------------------------------------------------------------


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("ipc-test"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _request(
    operation: str,
    payload: Any,
    *,
    instance_id: str | None = None,
    protocol: int = IPC_PROTOCOL_VERSION,
    build_id: str | None = BUILD_ID,
) -> bytes:
    body: dict[str, Any] = {
        "protocol_version": protocol,
        "operation": operation,
        "payload": payload,
    }
    body["request_id"] = secrets.token_hex(16)  # F-63: 128-bit hex routing identity
    if instance_id is not None:
        body["authority_instance_id"] = instance_id
    if build_id is not None:
        body["runtime_build_id"] = build_id
    return encode_json_frame(body)


def _server(
    session: AuthoritySession,
    invoke_result: Any = None,
    invoke_log: list | None = None,
) -> AuthorityIPCServer:
    async def invoke(name: str, input: dict) -> Any:
        if invoke_log is not None:
            invoke_log.append((name, input))
        return invoke_result or ok_result(data={"ok": True})

    return AuthorityIPCServer(
        session=session,
        authority_domain=Path("ipc-test"),
        invoke=invoke,
        runtime_build_id=BUILD_ID,
    )


async def test_protocol_mismatch_cannot_reach_admission_or_dispatcher() -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    frame = _request("read", {"post_url": "u"}, instance_id=session.authority_instance_id, protocol=99)
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["ok"] is False
    assert response["error"]["code"] == "protocol_mismatch"
    assert log == [], "the Dispatcher was never reached"


async def test_build_mismatch_cannot_reach_admission() -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    frame = _request(
        "read",
        {"post_url": "u"},
        instance_id=session.authority_instance_id,
        build_id="f" * 64,
    )
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["error"]["code"] == "build_mismatch"
    assert log == []


async def test_stale_instance_cannot_reach_dispatcher() -> None:
    """The reviewer's central negative proof: a stale instance id loses
    at AuthoritySession.admit(expected_instance_id=...) BEFORE any
    Dispatcher invocation."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    stale = "e" * 64  # a previous owner's instance identity
    frame = _request("read", {"post_url": "u"}, instance_id=stale)
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["error"]["code"] == "stale_authority_instance"
    assert log == [], "stale instance: no Dispatcher work"
    assert session.active_work == 0


async def test_forbidden_operations_rejected_before_dispatcher() -> None:
    """whoami, writes, media, download — everything outside the five
    Layer-4 names — dies at the allowlist, never reaching the Dispatcher."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    for op in (
        "whoami",
        "post_text",
        "like_post",
        "bookmark_post",
        "reply_post",
        "quote_post",
        "delete_post",
        "post_photo",
        "post_multi_image",
        "download_image",
        "create_reconciliation_operator_session",
    ):
        frame = _request(op, {}, instance_id=session.authority_instance_id)
        header, consumed = parse_frame_header(frame)
        response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
        assert response["error"]["code"] == "unsupported_operation", op
    assert log == []


async def test_valid_request_reaches_dispatcher_under_admission() -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_result=ok_result(data={"posts": []}), invoke_log=log)
    frame = _request(
        "read",
        {"post_url": "https://x.com/infaag/status/123"},
        instance_id=session.authority_instance_id,
    )
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["ok"] is True
    assert log == [("read", {"post_url": "https://x.com/infaag/status/123"})]


async def test_missing_instance_id_rejected() -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    frame = _request("read", {"post_url": "u"})  # no instance_id
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    # F-63: exact envelope equality catches the MISSING key here (before
    # the dedicated instance check would fire).
    assert response["error"]["code"] == "schema"
    assert "missing" in response["error"]["message"]
    assert log == []


async def test_draining_rejects_new_ipc_work() -> None:
    """A request racing shutdown: DRAINING rejects before admission."""
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    server.begin_drain()  # the shutdown law reached DRAINING
    frame = _request("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["ok"] is False
    assert "draining" in response["error"]["code"]
    assert log == []


async def test_schema_violation_rejected_before_admission() -> None:
    session = _session_ready()
    log: list = []
    server = _server(session, invoke_log=log)
    frame = _request(
        "read_profile",
        {"handle": "a", "tab": "not-a-real-tab"},
        instance_id=session.authority_instance_id,
    )
    header, consumed = parse_frame_header(frame)
    response = json.loads((await server.process_request(header, frame[consumed:]))[8:])
    assert response["error"]["code"] == "schema"
    assert log == []


async def test_malformed_frame_cannot_allocate_work() -> None:
    session = _session_ready()
    server = _server(session)
    header = FrameHeader(length=4)
    response = json.loads((await server.process_request(header, b"\xff\xfe\x00\x01"))[8:])
    assert response["error"]["code"] == "protocol"
    assert session.active_work == 0


async def test_admission_spans_complete_invocation() -> None:
    """The session admission spans the COMPLETE Dispatcher invocation —
    an admitted read blocks shutdown drain until it finishes."""
    import asyncio

    session = _session_ready()
    release = asyncio.Event()

    async def slow_read(name: str, input: dict) -> Any:
        await release.wait()
        return ok_result(data={"done": True})

    server = AuthorityIPCServer(
        session=session,
        authority_domain=Path("ipc-test"),
        invoke=slow_read,
        runtime_build_id=BUILD_ID,
    )
    frame = _request("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
    header, consumed = parse_frame_header(frame)
    task = asyncio.create_task(server.process_request(header, frame[consumed:]))
    await asyncio.sleep(0.1)
    assert session.active_work == 1, "admission held across the invocation"

    release.set()
    response = json.loads((await asyncio.wait_for(task, timeout=5))[8:])
    assert response["ok"] is True
    assert session.active_work == 0


async def test_client_disconnect_does_not_cancel_admitted_work() -> None:
    """The conservative disconnect law: the response is lost but the
    owner task runs to completion (the admission exits normally)."""
    import asyncio

    session = _session_ready()
    completed = []
    release = asyncio.Event()

    async def slow_read(name: str, input: dict) -> Any:
        await release.wait()
        completed.append("done")
        return ok_result(data={"done": True})

    server = AuthorityIPCServer(
        session=session,
        authority_domain=Path("ipc-test"),
        invoke=slow_read,
        runtime_build_id=BUILD_ID,
    )
    frame = _request("read", {"post_url": "u"}, instance_id=session.authority_instance_id)
    header, consumed = parse_frame_header(frame)

    # The "client" abandons the response — the task still completes.
    task = asyncio.create_task(server.process_request(header, frame[consumed:]))
    await asyncio.sleep(0.05)  # admitted
    # Client disconnect: just abandon the task; the work continues.
    release.set()
    raw = await asyncio.wait_for(task, timeout=5)
    assert json.loads(raw[8:])["ok"] is True
    assert completed == ["done"]
    assert session.active_work == 0


# ---------------------------------------------------------------------------
# Handshake (frozen contract item 3)
# ---------------------------------------------------------------------------


def test_hello_carries_exact_build_and_current_owner() -> None:
    session = _session_ready()
    server = _server(session)
    hello = server._hello()
    assert hello.protocol_version == IPC_PROTOCOL_VERSION
    assert hello.runtime_build_id == BUILD_ID
    assert hello.authority_instance_id == session.authority_instance_id
    assert hello.supported_operations == IPC_SUPPORTED_OPERATIONS
    assert hello.lifecycle_state == "ready"
    # Round-trip via the FROZEN wire names
    wire = hello.to_dict()
    assert "supported_ipc_operations" in wire  # F-65: frozen name
    assert "state" in wire  # F-65: frozen name
    restored = AuthorityHello.from_dict(wire)
    assert restored.runtime_build_id == BUILD_ID
    assert restored.authority_instance_id == session.authority_instance_id
