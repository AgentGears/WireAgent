"""M7 Layer 7 — media boundary across a real process death (lane 6).

T69 at the process boundary: an artifact_ref dies with its owner — a
successor owner's registry never knows it — while admitted same-owner
work keeps its media pinned until the safe terminal boundary. T68/T70
remain covered by the Layer-6 suites; this file adds only the
process-death dimension.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _stage_bytes(state_dir: Path, name: str = "photo.png", data: bytes = _PNG) -> None:
    staging = state_dir / "media-staging"
    staging.mkdir(parents=True, exist_ok=True)
    (staging / name).write_bytes(data)


# ---------------------------------------------------------------------------
# F-96A: artifact refs die with the owner (real ingest, real ref, real
# successor refusal, real re-ingest)
# ---------------------------------------------------------------------------


def test_artifact_ref_dies_with_owner_process(tmp_path: Path) -> None:
    """A REAL media_ingest over the wire mints a real artifact_ref; the
    owner dies uncleanly; the successor (a real new process) starts
    cleanly and the OLD ref is unknown to it — a post_photo carrying it
    dies with unknown_artifact BEFORE the Dispatcher; a fresh ingest on
    the successor mints a NEW ref. No replay authority survived."""
    scratch = _scratch(tmp_path, "refdeath")
    state_dir = scratch / "state"
    _stage_bytes(state_dir)

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "die"), "die")
    record = owner.result()
    assert record["started"] is True

    # REAL ingest over the wire.
    payload = scratch / "ingest.json"
    payload.write_text('{"staged_name": "photo.png"}', encoding="utf-8")
    ingester = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "media_ingest",
        payload,
        secrets.token_hex(16),
        "normal",
    )
    ingested = ingester.result()
    artifact = ingested["response"]["data"]["artifact"]
    old_ref = artifact["artifact_ref"]
    assert artifact["mime"] == "image/png"

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    srecord = successor.result()
    assert srecord["started"] is True
    try:
        # The old ref is DEAD on the successor: unknown before invoke.
        stale_payload = scratch / "stale.json"
        stale_payload.write_text(json.dumps({"text": "x", "artifact_ref": old_ref}), encoding="utf-8")
        stale = harness.start_worker(
            scratch,
            "ipc-request",
            Path(srecord["endpoint"]),
            srecord["build_id"],
            srecord["instance_id"],
            "post_photo",
            stale_payload,
            secrets.token_hex(16),
            "normal",
        )
        stale_response = stale.result()["response"]
        assert stale_response["ok"] is False
        assert stale_response["error"]["code"] == "unknown_artifact"

        # A fresh ingest mints a NEW ref (staged bytes survive as domain
        # state; refs never do).
        _stage_bytes(state_dir, "again.png", _PNG + bytes([1]))
        payload2 = scratch / "ingest2.json"
        payload2.write_text('{"staged_name": "again.png"}', encoding="utf-8")
        reingest = harness.start_worker(
            scratch,
            "ipc-request",
            Path(srecord["endpoint"]),
            srecord["build_id"],
            srecord["instance_id"],
            "media_ingest",
            payload2,
            secrets.token_hex(16),
            "normal",
        )
        new_artifact = reingest.result()["response"]["data"]["artifact"]
        assert new_artifact["artifact_ref"] != old_ref
    finally:
        harness.open_gate(scratch, "done")
        successor.wait()


# ---------------------------------------------------------------------------
# F-96B: crash DURING real ingress — .tmp residue only, no partial object
# ---------------------------------------------------------------------------


def test_crash_during_real_ingress_leaves_tmp_only(tmp_path: Path) -> None:
    """A REAL ingest starts on a full owner whose acquisition is wrapped
    to die between the bounded temporary copy and the atomic publish.
    After the crash: the artifact root holds the .tmp residue and NO
    <sha256>.bin; a successor starts safely on the same domain."""
    scratch = _scratch(tmp_path, "ingresscrash")
    state_dir = scratch / "state"
    _stage_bytes(state_dir)

    owner = harness.start_worker(scratch, "ingest-crash", state_dir, harness.gate(scratch, "die"))
    record = owner.result()
    assert record["started"] is True

    # Fire the real ingest over the wire (its client will be killed —
    # the owner dies mid-request).
    payload = scratch / "ingest.json"
    payload.write_text('{"staged_name": "photo.png"}', encoding="utf-8")
    ingester = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "media_ingest",
        payload,
        secrets.token_hex(16),
        "normal",
    )

    # The owner reached the pre-publish barrier.
    deadline = time.monotonic() + 15
    marker = owner.result_path.with_suffix(".temp")
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert marker.exists(), "the real acquisition reached its temp copy"

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN
    ingester.kill()  # its response can never exist

    artifacts = state_dir / "media-artifacts"
    assert list(artifacts.glob("*.bin")) == [], "NO content-addressed object was published"
    assert list(artifacts.glob("*.tmp")), "the bounded temp residue is exactly what remains"

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    assert successor.result()["started"] is True
    harness.open_gate(scratch, "done")
    successor.wait()


# ---------------------------------------------------------------------------
# F-96C: pin through drain (real ref, real admitted media mutation)
# ---------------------------------------------------------------------------


def test_pinned_media_survives_stop_until_work_terminalizes(tmp_path: Path) -> None:
    """A REAL ingest mints the ref; an ADMITTED post_photo blocks at the
    controlled execution barrier WITH its artifact pinned. The owner's
    stop sequence begins while pinned: the artifact file SURVIVES, the
    owner stays alive and the domain stays locked; after the barrier
    releases — the admitted media work terminalizing — the post-drain
    retention pass reclaims the artifact and the owner exits cleanly."""
    scratch = _scratch(tmp_path, "pindrain")
    state_dir = scratch / "state"
    _stage_bytes(state_dir)

    owner = harness.start_worker(
        scratch,
        "full-owner-media-block",
        state_dir,
        harness.gate(scratch, "stop"),
        harness.gate(scratch, "exec"),
        scratch / "admissions.ndjson",
    )
    record = owner.result()
    assert record["started"] is True

    payload = scratch / "ingest.json"
    payload.write_text('{"staged_name": "photo.png"}', encoding="utf-8")
    ingester = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "media_ingest",
        payload,
        secrets.token_hex(16),
        "normal",
    )
    artifact = ingester.result()["response"]["data"]["artifact"]
    owner_path = Path(state_dir) / "media-artifacts" / f"{artifact['sha256']}.bin"
    assert owner_path.exists()

    # Preview then confirm; the confirm admits and blocks, PINNED.
    preview_env = scratch / "preview.json"
    preview_env.write_text(
        json.dumps({"text": "pinned", "artifact_ref": artifact["artifact_ref"]}),
        encoding="utf-8",
    )
    previewer = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_photo",
        preview_env,
        secrets.token_hex(16),
        "normal",
    )
    token = previewer.result()["response"]["data"]["data"]["confirmation_token"]

    confirm_env = scratch / "confirm.json"
    confirm_env.write_text(
        json.dumps({"text": "pinned", "artifact_ref": artifact["artifact_ref"], "confirmation_token": token}),
        encoding="utf-8",
    )
    confirmer = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_photo",
        confirm_env,
        secrets.token_hex(16),
        "disconnect",
    )
    assert confirmer.result()["sent"] is True

    # Begin the stop while the media work is blocked and pinned.
    harness.open_gate(scratch, "stop")
    deadline = time.monotonic() + 15
    stopping = owner.result_path.with_suffix(".stopping")
    while not stopping.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() is None, "the owner stays alive draining the pinned media work"
    assert owner_path.exists(), "the pinned artifact survives the drain window"

    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True

    # Terminal boundary: release; the artifact is reclaimed at shutdown.
    harness.open_gate(scratch, "exec")
    assert owner.wait(timeout=60) == harness.EXIT_OK
    assert not owner_path.exists(), "post-drain retention reclaimed the unpinned artifact"
