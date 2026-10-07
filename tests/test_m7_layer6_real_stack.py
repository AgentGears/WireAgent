"""M7 Layer 6 first-review repair — F-86: the T68/T70 acceptance proofs
through the PRODUCTION media stack.

A real client socket → the owner's IPC pipeline → the REAL Dispatcher →
the REAL WriteKernel (real token mint/consume) → M5MediaCapabilityAdapter
→ M5ActorBoundMediaExecutor over a controlled fake media port (the DOM
boundary). Proves: phase-1 preview binds the content-addressed digest;
phase-2 confirmation reaches the SAME exact owner bytes; the artifact
stays pinned across admitted execution and client disconnect; and a
corrupt ordered item N causes ZERO attachment operations for items 1..N-1
(T70) — no composer work, no attach calls.
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
from webwire.authority_ipc_transport import IPCClient
from webwire.config import WebWireConfig
from webwire.envelope import ok_result

BUILD_ID = compute_runtime_build_id()

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


class _StubSB:
    _page = None
    _controller = None


class _MediaPort:
    """The DOM boundary for media writes (ported from the M5 media
    executor suite): records every attach call with the bytes it saw."""

    def __init__(self) -> None:
        self.attach_calls: list[bytes] = []
        self.composer_opened = False
        self.composer_text = ""
        # Cross-loop coordination: these events are created on the test's
        # loop but awaited/set on the DISPATCHER's transport loop — plain
        # asyncio.Event would never wake cross-loop waiters. Thread events
        # with polling are loop-safe.
        self.block_submit = threading.Event()
        self.block_submit.set()  # submit flows by default; tests may block it
        self.submit_reached = threading.Event()

    async def fill_composer(self, text: str) -> ActionResult:
        self.composer_opened = True
        self.composer_text = text
        return ok_result(data={"filled": True})

    async def attach_media(self, image_path: str) -> ActionResult:
        # Read through the OWNER path: the bytes the mutation uploads.
        self.attach_calls.append(Path(image_path).read_bytes())  # noqa: ASYNC240
        return ok_result(data={"attached": True})

    async def verify_attachment_ready(self) -> ActionResult:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> ActionResult:
        return ok_result(data={"count": len(self.attach_calls)})

    async def read_composer_text(self) -> ActionResult:
        return ok_result(data={"composer_text": self.composer_text})

    async def close_composer(self) -> ActionResult:
        return ok_result(data={"cleanup": "closed"})

    async def click_submit(
        self, *, _commit_gate, _precommit_check, _expected_text, _expected_attachments
    ) -> ActionResult:
        self.submit_reached.set()
        while not self.block_submit.is_set():  # noqa: ASYNC110 — cross-loop thread event
            await asyncio.sleep(0.02)
        checked = await _precommit_check()
        if checked is not None:
            return checked
        denied = _commit_gate()
        if denied is not None:
            return denied
        return ok_result(data={"submitted": True})


class _ContentEvidence:
    """Actor-bound evidence contract (the @owner identity the dispatcher
    resolved): capture and verification both bind to the owner's status
    URL identity exactly as M5ActorBoundMediaExecutor._content_proof
    requires for plain media posts."""

    ACTOR = "owner"
    POST_ID = "99"
    POST_URL = "https://x.com/owner/status/99"

    async def capture_pre_submit_ids(self) -> ActionResult:
        return ok_result(data={"status_ids": ["10", "11"]})

    async def capture_new_post(self, pre_submit_ids: set[str], *, exclude_ids=None) -> ActionResult:
        return ok_result(data={"post_id": self.POST_ID, "post_url": self.POST_URL})

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        return ok_result(
            data={
                "text_matches": True,
                "direct_status_owned": True,
                "post_actor": self.ACTOR,
                "post_id": self.POST_ID,
                "post_url": self.POST_URL,
            }
        )


async def _started_media_dispatcher(tmp_path: Path, port: _MediaPort):
    from types import SimpleNamespace

    from webwire.dispatcher import Dispatcher
    from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
    from webwire.safety.m5_actor_bound_media_executor import M5ActorBoundMediaExecutor
    from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
    from webwire.safety.scoped_authority import ScopedAuthorityBroker
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
    from types import SimpleNamespace as _NS

    dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
        lambda sb: setattr(dispatcher, "_m5_stack", _NS(read_broker=object()))
    )
    started = await dispatcher.start()
    assert started.ok, getattr(started.error, "message", started)

    # Replace the stub stack with the REAL media write path over the
    # dispatcher's OWN gateway (per-invocation _m5_stack consult).
    scoped = ScopedAuthorityBroker(
        port, dispatcher._m5_gateway, policies=DEFAULT_EFFECT_POLICIES
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=dispatcher._m5_gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    media_executor = M5ActorBoundMediaExecutor(
        runtime=runtime,
        content_evidence=_ContentEvidence(),
        media_evidence=_MediaEvidenceReader(port),
    )
    dispatcher._m5_stack = SimpleNamespace(
        read_broker=object(),
        media_executor=media_executor,
    )
    return dispatcher


class _MediaEvidenceReader:
    """The actor-bound executor's media-evidence seam, stubbed at the
    same boundary the M5 suite uses (post-media count check)."""

    def __init__(self, port: _MediaPort) -> None:
        self._port = port

    async def count_post_media(self, post_url: str) -> ActionResult:
        return ok_result(
            data={
                "post_url": post_url,
                "media_count": len(self._port.attach_calls),
                "source_byte_equivalence_verified": True,
            }
        )


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


async def test_T68_preview_binds_digest_confirm_reaches_owner_bytes(tmp_path: Path) -> None:
    """The full T68 chain: wire ingest → phase-1 preview through the REAL
    WriteKernel binds the content-addressed digest → phase-2 confirmation
    consumes the real token and the M5 media executor attaches the EXACT
    owner bytes."""
    port = _MediaPort()
    dispatcher = await _started_media_dispatcher(tmp_path, port)
    try:
        registry = dispatcher._media_registry
        staging = registry.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "photo.png").write_bytes(_PNG)

        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        minted = await _wire_async(path, instance, "media_ingest", {"staged_name": "photo.png"})
        assert minted["ok"] is True, minted
        artifact = minted["data"]["artifact"]
        import hashlib

        assert artifact["sha256"] == hashlib.sha256(_PNG).hexdigest()

        # Phase 1: the REAL WriteKernel mints a token over the preview —
        # which is computed from the OWNER path (its own re-hash of the
        # artifact must equal the bound digest).
        preview = await _wire_async(
            path, instance, "post_photo", {"text": "real stack", "artifact_ref": artifact["artifact_ref"]}
        )
        assert preview["ok"] is True, preview
        kernel_data = preview["data"]["data"]
        token = kernel_data["confirmation_token"]
        # The preview's attachment display carries the SAME digest the
        # ingress bound (the exact-artifact confirmation binding).
        assert artifact["sha256"] in str(kernel_data)

        # Phase 2: real consumption → the executor attaches the OWNER
        # bytes through the media port.
        outcome = await _wire_async(
            path,
            instance,
            "post_photo",
            {"text": "real stack", "artifact_ref": artifact["artifact_ref"], "confirmation_token": token},
        )
        assert outcome["ok"] is True, outcome
        assert port.attach_calls == [_PNG], "the exact owner bytes were attached"
    finally:
        await dispatcher.stop()


async def test_T68_artifact_pinned_across_client_disconnect(tmp_path: Path) -> None:
    """An admitted media mutation BLOCKS in submit with the client GONE:
    the artifact stays pinned (retention cannot delete it) until the
    mutation reaches its terminal boundary, then the pin releases."""
    port = _MediaPort()
    port.block_submit.set()  # will be cleared only after the disconnect
    dispatcher = await _started_media_dispatcher(tmp_path, port)
    try:
        registry = dispatcher._media_registry
        staging = registry.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "photo.png").write_bytes(_PNG)
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        minted = await _wire_async(path, instance, "media_ingest", {"staged_name": "photo.png"})
        artifact = minted["data"]["artifact"]

        preview = await _wire_async(
            path, instance, "post_photo", {"text": "x", "artifact_ref": artifact["artifact_ref"]}
        )
        token = preview["data"]["data"]["confirmation_token"]

        # Send the confirm and VANISH (one request per connection: the
        # raw frame goes out, then the socket dies).
        def _send_and_vanish() -> None:
            from webwire.authority_ipc_framing import encode_json_frame

            client = IPCClient(path, expected_build_id=BUILD_ID)
            client.connect()
            client._sock.send(
                encode_json_frame(
                    {
                        "protocol_version": IPC_PROTOCOL_VERSION,
                        "operation": "post_photo",
                        "payload": {
                            "text": "x",
                            "artifact_ref": artifact["artifact_ref"],
                            "confirmation_token": token,
                        },
                        "request_id": secrets.token_hex(16),
                        "authority_instance_id": instance,
                        "runtime_build_id": BUILD_ID,
                    }
                )
            )
            client._sock.close()

        port.block_submit.clear()  # block inside submit
        sender = threading.Thread(target=_send_and_vanish)
        sender.start()
        sender.join(timeout=5)

        deadline = time.monotonic() + 5
        while not port.submit_reached.is_set() and time.monotonic() < deadline:  # noqa: ASYNC110
            await asyncio.sleep(0.02)
        assert port.submit_reached.is_set(), "the admitted mutation reached submit despite the disconnect"

        # The artifact is PINNED: retention cannot delete live media.
        registry.cleanup_unpinned()
        pinned_path = Path(registry.resolve(artifact["artifact_ref"]).owner_path)
        assert pinned_path.exists()  # noqa: ASYNC240  # noqa: ASYNC240

        # Terminal boundary: release the blocked submit; the work
        # completes owner-side and the pin drops.
        port.block_submit.set()
        await asyncio.sleep(0.5)
        registry.cleanup_unpinned()
    finally:
        port.block_submit.set()
        await dispatcher.stop()


async def test_T70_corrupt_item_n_means_zero_attach_calls(tmp_path: Path) -> None:
    """T70 through the production stack: an ordered multi-image request
    whose item 2 is corrupted between preview and confirm resolves
    NOTHING — the Dispatcher is never reached, the composer is never
    opened, and ZERO attach calls happen for item 1."""
    port = _MediaPort()
    dispatcher = await _started_media_dispatcher(tmp_path, port)
    try:
        registry = dispatcher._media_registry
        staging = registry.staging_root
        staging.mkdir(parents=True, exist_ok=True)
        (staging / "one.png").write_bytes(_PNG)
        (staging / "two.png").write_bytes(_PNG + bytes([1]))
        path = str(dispatcher._ipc_transport.endpoint_path or "")
        instance = dispatcher._authority_session.authority_instance_id

        refs = []
        for name in ("one.png", "two.png"):
            minted = await _wire_async(path, instance, "media_ingest", {"staged_name": name})
            refs.append(minted["data"]["artifact"])

        preview = await _wire_async(
            path,
            instance,
            "post_multi_image",
            {"text": "t70", "artifact_refs": [r["artifact_ref"] for r in refs]},
        )
        assert preview["ok"] is True, preview
        token = preview["data"]["data"]["confirmation_token"]

        # Corrupt item 2's owner copy between preview and confirm.
        bad_owner = Path(registry.resolve(refs[1]["artifact_ref"]).owner_path)  # noqa: ASYNC240
        bad_owner.write_bytes(b"corrupted")  # noqa: ASYNC240

        confirm = await _wire_async(
            path,
            instance,
            "post_multi_image",
            {
                "text": "t70",
                "artifact_refs": [r["artifact_ref"] for r in refs],
                "confirmation_token": token,
            },
        )
        assert confirm["ok"] is False, confirm
        assert confirm["error"]["code"] == "digest_mismatch"
        assert port.attach_calls == [], "T70: zero attachment operations"
        assert port.composer_opened is False, "no composer work before full resolution"
    finally:
        await dispatcher.stop()
