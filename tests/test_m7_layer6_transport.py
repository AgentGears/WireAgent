"""M7 Layer 6 — the media referent model over the REAL transport.

Platform-neutral (domain sockets and named pipes alike): a real client
ingests a staged artifact over the wire, mints the opaque ref, drives a
media write whose Dispatcher-visible payload carries the OWNER path,
proves stale-owner refs die, and proves the ordered all-or-nothing law
on the wire.
"""

from __future__ import annotations

import asyncio
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from webwire.authority_ipc_protocol import IPC_PROTOCOL_VERSION, compute_runtime_build_id
from webwire.authority_ipc_server import AuthorityIPCServer
from webwire.authority_ipc_transport import IPCClient, IPCTransportServer
from webwire.authority_media_ingress import MediaArtifactRegistry
from webwire.authority_session import AuthoritySession
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def _session_ready() -> AuthoritySession:
    session = AuthoritySession(authority_domain=Path("layer6-transport"))
    session.register_revoker(lambda: None)
    session.activate()
    return session


def _stack(tmp_path: Path, invoke: Any):
    session = _session_ready()
    registry = MediaArtifactRegistry(
        staging_root=tmp_path / "staging",
        artifact_root=tmp_path / "artifacts",
        authority_instance_id=session.authority_instance_id,
    )
    loop = asyncio.new_event_loop()

    def _run_loop() -> None:
        asyncio.set_event_loop(loop)
        loop.run_forever()

    threading.Thread(target=_run_loop, daemon=True).start()
    ipc = AuthorityIPCServer(
        session=session,
        authority_domain=tmp_path,
        invoke=invoke,
        runtime_build_id=BUILD_ID,
        media_registry=registry,
    )
    transport = IPCTransportServer(ipc_server=ipc, authority_domain=tmp_path, loop=loop)
    transport.start()
    return session, registry, ipc, transport, loop, str(transport.endpoint_path or "")


def _teardown(transport, loop) -> None:
    transport.stop()
    loop.call_soon_threadsafe(loop.stop)
    time.sleep(0.2)  # noqa: ASYNC251
    loop.close()


def _wire(path: str, instance: str, operation: str, payload: dict) -> dict:
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


async def _wire_async(path: str, instance: str, operation: str, payload: dict) -> dict:
    return await asyncio.to_thread(_wire, path, instance, operation, payload)


def _stage(registry: MediaArtifactRegistry, name: str, data: bytes = _PNG) -> None:
    registry.staging_root.mkdir(parents=True, exist_ok=True)
    (registry.staging_root / name).write_bytes(data)


async def test_wire_ingest_then_write_uses_owner_path(tmp_path: Path) -> None:
    """The full referent chain on the wire: ingest mints the opaque ref
    (preview-grade metadata, never the staged name); the media write's
    Dispatcher-visible payload carries the OWNER-side content-addressed
    path; the raw ref never reaches a capability."""
    seen: list[dict] = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append({"name": name, **payload})
        return ok_result(data={"posted": True})

    session, registry, ipc, transport, loop, path = _stack(tmp_path, invoke)
    try:
        _stage(registry, "photo.png")
        ingested = await _wire_async(
            path, session.authority_instance_id, "media_ingest", {"staged_name": "photo.png"}
        )
        assert ingested["ok"] is True, ingested
        artifact = ingested["data"]["artifact"]
        assert artifact["mime"] == "image/png"

        response = await _wire_async(
            path,
            session.authority_instance_id,
            "post_photo",
            {"text": "from the wire", "artifact_ref": artifact["artifact_ref"]},
        )
        assert response["ok"] is True, response
        assert seen[0]["name"] == "post_photo"
        assert seen[0]["image_path"].endswith(f"{artifact['sha256']}.bin")
        assert "artifact_ref" not in seen[0]
    finally:
        _teardown(transport, loop)


async def test_wire_multi_image_all_or_nothing(tmp_path: Path) -> None:
    """Ordered multi-image over the wire: two good refs in ORDER both
    resolve to owner paths in order; a set containing one corrupted ref
    resolves NOTHING and the Dispatcher is never reached."""
    seen: list[dict] = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append({"name": name, **payload})
        return ok_result(data={"posted": True})

    session, registry, ipc, transport, loop, path = _stack(tmp_path, invoke)
    try:
        _stage(registry, "one.png")
        _stage(registry, "two.png", data=_PNG + bytes([1]))
        _stage(registry, "bad.png", data=_PNG + bytes([2]))
        refs = []
        for name in ("one.png", "two.png", "bad.png"):
            minted = await _wire_async(
                path, session.authority_instance_id, "media_ingest", {"staged_name": name}
            )
            refs.append(minted["data"]["artifact"])

        ok = await _wire_async(
            path,
            session.authority_instance_id,
            "post_multi_image",
            {"text": "ordered", "artifact_refs": [refs[0]["artifact_ref"], refs[1]["artifact_ref"]]},
        )
        assert ok["ok"] is True, ok
        assert [Path(p).stem for p in seen[0]["image_paths"]] == [
            refs[0]["sha256"],
            refs[1]["sha256"],
        ], "order preserved"

        # Corrupt the third artifact's owner copy, then order it into a set.

        bad_owner = tmp_path / "artifacts" / f"{refs[2]['sha256']}.bin"
        bad_owner.write_bytes(b"corrupted")
        failed = await _wire_async(
            path,
            session.authority_instance_id,
            "post_multi_image",
            {"text": "partial", "artifact_refs": [refs[0]["artifact_ref"], refs[2]["artifact_ref"]]},
        )
        assert failed["ok"] is False
        assert failed["error"]["code"] == "digest_mismatch"
        assert len(seen) == 1, "the failing set never reached the Dispatcher"
    finally:
        _teardown(transport, loop)


async def test_wire_raw_path_and_unknown_ref_are_stable_errors(tmp_path: Path) -> None:
    """A raw client filesystem path dies at schema over the wire; an
    unknown (expired/restarted) ref dies with unknown_artifact before the
    Dispatcher — both stable codes, never internal."""
    seen: list = []

    async def invoke(name: str, payload: dict) -> Any:
        seen.append(name)
        return ok_result(data={})

    session, registry, ipc, transport, loop, path = _stack(tmp_path, invoke)
    try:
        raw = await _wire_async(
            path,
            session.authority_instance_id,
            "post_photo",
            {"text": "x", "image_path": "C:/raw/client/path.png"},
        )
        assert raw["error"]["code"] == "schema"

        unknown = await _wire_async(
            path,
            session.authority_instance_id,
            "post_photo",
            {"text": "x", "artifact_ref": "f" * 32},
        )
        assert unknown["error"]["code"] == "unknown_artifact"
        assert seen == []
    finally:
        _teardown(transport, loop)
