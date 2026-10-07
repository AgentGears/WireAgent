"""M7 Layer 6 — the media IPC surface at the pipeline level.

The six media-backed writes join the advertised surface ONLY through
opaque artifact_refs minted by media_ingest; raw image_path strings die
at the schema; refs resolve ALL-OR-NOTHING to OWNER-side paths before
the Dispatcher is reachable; admitted work PINS its artifacts; refs are
instance-scoped and die with the owner.
"""

from __future__ import annotations

import asyncio
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
from webwire.authority_media_ingress import MediaArtifactRegistry
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()
INSTANCE = "i" * 64

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

MEDIA_OPS = frozenset(
    {
        "media_ingest",
        "post_photo",
        "reply_photo",
        "quote_photo",
        "post_multi_image",
        "reply_multi_image",
        "quote_multi_image",
    }
)


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("layer6-test"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _registry(tmp_path: Path, instance: str = INSTANCE) -> MediaArtifactRegistry:
    return MediaArtifactRegistry(
        staging_root=tmp_path / "staging",
        artifact_root=tmp_path / "artifacts",
        authority_instance_id=instance,
    )


def _stage(registry: MediaArtifactRegistry, name: str, data: bytes = _PNG) -> None:
    registry.staging_root.mkdir(parents=True, exist_ok=True)
    (registry.staging_root / name).write_bytes(data)


def _server(
    session: AuthoritySession,
    tmp_path: Path,
    *,
    registry: MediaArtifactRegistry | None = None,
    invoke: Any = None,
) -> AuthorityIPCServer:
    async def _default_invoke(name: str, payload: dict) -> Any:
        return ok_result(data={"ok": True})

    return AuthorityIPCServer(
        session=session,
        authority_domain=Path("layer6-test"),
        invoke=invoke or _default_invoke,
        runtime_build_id=BUILD_ID,
        media_registry=registry,
    )


def _frame(operation: str, payload: Any, *, instance: str = INSTANCE, request_id: str | None = None) -> bytes:
    body: dict[str, Any] = {
        "protocol_version": IPC_PROTOCOL_VERSION,
        "operation": operation,
        "payload": payload,
        "request_id": request_id or secrets.token_hex(16),
        "authority_instance_id": instance,
        "runtime_build_id": BUILD_ID,
    }
    return encode_json_frame(body)


async def _respond(server: AuthorityIPCServer, frame: bytes) -> dict[str, Any]:
    header, consumed = parse_frame_header(frame)
    return json.loads((await server.process_request(header, frame[consumed:]))[8:])


async def _ask(
    server: AuthorityIPCServer, session: AuthoritySession, operation: str, payload: Any
) -> dict[str, Any]:
    """One pipeline request bound to THIS session's real instance id."""
    return await _respond(server, _frame(operation, payload, instance=session.authority_instance_id))


# ---------------------------------------------------------------------------
# The advertised surface (T66-flip with qualified ingress)
# ---------------------------------------------------------------------------


def test_media_ops_advertised_ingress_qualified_download_still_forbidden() -> None:
    """The six media writes + media_ingest join the surface now that the
    ingress contract qualifies; download_image (the local-output side)
    remains unadvertised until its own output contract exists."""
    assert MEDIA_OPS <= IPC_SUPPORTED_OPERATIONS
    assert "download_image" not in IPC_SUPPORTED_OPERATIONS


# ---------------------------------------------------------------------------
# media_ingest over the pipeline
# ---------------------------------------------------------------------------


async def test_media_ingest_mints_ref_over_pipeline(tmp_path: Path) -> None:
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "photo.png")
    server = _server(session, tmp_path, registry=registry)

    response = await _ask(server, session, "media_ingest", {"staged_name": "photo.png"})
    assert response["ok"] is True, response
    artifact = response["data"]["artifact"]
    assert artifact["mime"] == "image/png"
    assert artifact["sha256"] and len(artifact["sha256"]) == 64
    assert artifact["artifact_ref"] and artifact["artifact_ref"] != "photo.png"


async def test_media_ingest_failures_are_stable_wire_codes(tmp_path: Path) -> None:
    session = _session_ready()
    registry = _registry(tmp_path)
    server = _server(session, tmp_path, registry=registry)

    traversal = await _ask(server, session, "media_ingest", {"staged_name": "../escape.png"})
    assert traversal["error"]["code"] == "invalid_staged_name"

    missing = await _ask(server, session, "media_ingest", {"staged_name": "nope.png"})
    assert missing["ok"] is False
    assert missing["error"]["code"] in ("not_regular_file", "media_validation_failed")


async def test_media_ingest_without_registry_refused(tmp_path: Path) -> None:
    session = _session_ready()
    server = _server(session, tmp_path, registry=None)
    response = await _ask(server, session, "media_ingest", {"staged_name": "x.png"})
    assert response["error"]["code"] == "media_unavailable"


# ---------------------------------------------------------------------------
# The six writes: opaque refs only, owner-path translation, pinning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("post_photo", {"text": "hi", "artifact_ref": "REF"}),
        ("reply_photo", {"post_url": "https://x.com/a/1", "text": "r", "artifact_ref": "REF"}),
        ("quote_photo", {"target_post_id": "1", "text": "q", "artifact_ref": "REF"}),
        ("post_multi_image", {"artifact_refs": ["A", "B"]}),
        ("reply_multi_image", {"post_url": "u", "text": "r", "artifact_refs": ["A", "B"]}),
        ("quote_multi_image", {"target_post_id": "1", "text": "q", "artifact_refs": ["A", "B"]}),
    ],
)
async def test_media_write_schemas_accept_ref_shapes(operation: str, payload: dict) -> None:
    session = _session_ready()
    registry = _registry(tmp_path := Path("layer6-schema-probe"))
    registry.staging_root.mkdir(parents=True, exist_ok=True)
    try:
        (registry.staging_root / "a.png").write_bytes(_PNG)
        (registry.staging_root / "b.png").write_bytes(_PNG + bytes([1]))
        minted = [registry.ingest("a.png"), registry.ingest("b.png")]
        resolved_payload = dict(payload)
        if "artifact_ref" in resolved_payload:
            resolved_payload["artifact_ref"] = minted[0].ref
        else:
            resolved_payload["artifact_refs"] = [m.ref for m in minted]
        server = _server(session, tmp_path, registry=registry)
        response = await _ask(server, session, operation, resolved_payload)
        assert response["ok"] is True, response
    finally:
        import shutil

        shutil.rmtree(tmp_path, ignore_errors=True)


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("post_photo", {"text": "x", "image_path": "C:/raw/client/path.png"}),  # raw path!
        ("post_multi_image", {"text": "x", "image_paths": ["C:/a.png"]}),  # raw paths!
        ("post_multi_image", {"text": "x", "artifact_refs": []}),  # empty ordered set
        (
            "post_multi_image",
            {"text": "x", "artifact_refs": ["A"] * 5},
        ),  # over the 4-item bound
        ("post_photo", {"text": "x"}),  # no artifact at all
    ],
)
async def test_media_write_schemas_reject_raw_paths_and_bad_shapes(operation: str, payload: dict) -> None:
    """A client filesystem string can never become mutation authority
    through IPC: raw image_path(s) die as unknown fields before any
    capability compose."""
    session = _session_ready()
    server = _server(session, Path("layer6-schema-probe2"), registry=_registry(Path("layer6-schema-probe2")))
    response = await _respond(server, _frame(operation, payload))
    assert response["ok"] is False
    assert response["error"]["code"] == "schema"


async def test_media_write_receives_owner_path_not_ref(tmp_path: Path) -> None:
    """The Dispatcher-visible payload carries the OWNER-side
    content-addressed path; the opaque ref never reaches a capability."""
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "photo.png")
    minted = registry.ingest("photo.png")
    seen: list[dict] = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append({"name": name, **payload})
        return ok_result(data={"posted": True})

    server = _server(session, tmp_path, registry=registry, invoke=invoke)
    response = await _ask(server, session, "post_photo", {"text": "hello", "artifact_ref": minted.ref})
    assert response["ok"] is True
    assert seen[0]["name"] == "post_photo"
    assert seen[0]["image_path"] == minted.owner_path  # noqa: ASYNC240
    assert "artifact_ref" not in seen[0]


async def test_media_write_resolves_all_or_nothing_before_invoke(tmp_path: Path) -> None:
    """One bad ref in an ordered multi-image request resolves NOTHING —
    the Dispatcher is never reached (no partial media authority)."""
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "one.png")
    _stage(registry, "bad.png", data=_PNG + bytes([1]))
    good = registry.ingest("one.png")
    bad = registry.ingest("bad.png")
    Path(bad.owner_path).write_bytes(b"corrupted")  # noqa: ASYNC240
    seen: list = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append(name)
        return ok_result(data={})

    server = _server(session, tmp_path, registry=registry, invoke=invoke)
    response = await _ask(
        server, session, "post_multi_image", {"text": "x", "artifact_refs": [good.ref, bad.ref]}
    )
    assert response["ok"] is False
    assert response["error"]["code"] == "digest_mismatch"
    assert seen == [], "all-or-nothing: no Dispatcher invocation"


async def test_unknown_ref_is_a_stable_error_before_invoke(tmp_path: Path) -> None:
    session = _session_ready()
    registry = _registry(tmp_path)
    seen: list = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append(name)
        return ok_result(data={})

    server = _server(session, tmp_path, registry=registry, invoke=invoke)
    response = await _ask(server, session, "post_photo", {"text": "x", "artifact_ref": "f" * 32})
    assert response["error"]["code"] == "unknown_artifact"
    assert seen == []


async def test_admitted_media_work_pins_its_artifacts(tmp_path: Path) -> None:
    """While an admitted media mutation executes, its artifact is PINNED:
    retention cannot delete live media; the pin releases at the terminal
    boundary."""
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "photo.png")
    minted = registry.ingest("photo.png")
    inside_invoke = asyncio.Event()
    release = asyncio.Event()

    async def invoke(name: str, payload: dict) -> Any:
        inside_invoke.set()
        await release.wait()
        return ok_result(data={"posted": True})

    server = _server(session, tmp_path, registry=registry, invoke=invoke)
    task = asyncio.create_task(_ask(server, session, "post_photo", {"text": "x", "artifact_ref": minted.ref}))
    await inside_invoke.wait()
    try:
        registry.cleanup_unpinned()
        assert Path(minted.owner_path).exists(), "pinned media survives retention"  # noqa: ASYNC240
        assert registry.resolve(minted.ref).sha256 == minted.sha256
    finally:
        release.set()
        response = await task
    assert response["ok"] is True
    # Terminal boundary reached: the pin is gone; retention may reclaim.
    registry.cleanup_unpinned()
    with pytest.raises(Exception, match="unknown_artifact"):  # noqa: B017
        registry.resolve(minted.ref)


# ---------------------------------------------------------------------------
# F-84: media_release over the wire; F-85: alt-text withheld
# ---------------------------------------------------------------------------


async def test_media_release_over_the_wire(tmp_path: Path) -> None:
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "photo.png")
    minted = registry.ingest("photo.png")
    server = _server(session, tmp_path, registry=registry)

    released = await _ask(
        server, session, "media_release", {"artifact_ref": minted.ref}
    )
    assert released["ok"] is True and released["data"]["released"] is True

    gone = await _ask(server, session, "post_photo", {"text": "x", "artifact_ref": minted.ref})
    assert gone["error"]["code"] == "unknown_artifact"

    unknown = await _ask(server, session, "media_release", {"artifact_ref": "f" * 32})
    assert unknown["error"]["code"] == "unknown_artifact"


async def test_pinned_artifact_refuses_wire_release(tmp_path: Path) -> None:
    """An artifact pinned by live admitted work cannot be released from
    the wire while the mutation executes."""
    session = _session_ready()
    registry = _registry(tmp_path)
    _stage(registry, "photo.png")
    minted = registry.ingest("photo.png")
    inside_invoke = asyncio.Event()
    release = asyncio.Event()

    async def invoke(name: str, payload: dict) -> Any:
        inside_invoke.set()
        await release.wait()
        return ok_result(data={"posted": True})

    server = _server(session, tmp_path, registry=registry, invoke=invoke)
    task = asyncio.create_task(
        _ask(server, session, "post_photo", {"text": "x", "artifact_ref": minted.ref})
    )
    await inside_invoke.wait()
    try:
        refused = await _ask(server, session, "media_release", {"artifact_ref": minted.ref})
        assert refused["error"]["code"] == "artifact_pinned"
    finally:
        release.set()
        assert (await task)["ok"] is True


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("post_photo", {"text": "x", "artifact_ref": "R", "alt_text": "a description"}),
        (
            "post_multi_image",
            {"artifact_refs": ["A", "B"], "alt_texts": ["one", "two"]},
        ),
    ],
)
async def test_alt_text_fields_withheld_from_ipc(operation: str, payload: dict) -> None:
    """F-85: alt-text is NOT an IPC field until real upload support
    exists — the contract must not accept inputs the execution path
    silently ignores."""
    session = _session_ready()
    server = _server(session, Path("layer6-alt-probe"), registry=None)
    response = await _ask(server, session, operation, payload)
    assert response["ok"] is False
    assert response["error"]["code"] == "schema"
