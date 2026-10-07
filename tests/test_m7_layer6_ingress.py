"""M7 Layer 6 — owner-side artifact ingress (the frozen boundary).



The artifact REFERENT MODEL is the round's center of gravity, not the six

capability names: the client can never turn an arbitrary filesystem

string into a media mutation by placing it in an IPC payload. Bytes are

ingested from the OWNER-controlled staging root into an owner-side

content-addressed store; validation (the EXISTING media pipeline —

upload roots, regular-file/no-symlink, magic-byte MIME, size, dimensions,

SHA-256, EXIF) runs BEFORE any preview/token authority; the minted

artifact_ref is opaque, instance-scoped, and ephemeral — never M5/M6

authority and never a durable replay credential.

"""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.authority_media_ingress import (
    MediaArtifactRegistry,
    MediaIngressError,
)

# A real 1x1 transparent PNG (magic bytes pass the existing validator).

_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


INSTANCE = "i" * 64


def _registry(tmp_path: Path, instance: str = INSTANCE) -> MediaArtifactRegistry:

    return MediaArtifactRegistry(
        staging_root=tmp_path / "staging",
        artifact_root=tmp_path / "artifacts",
        authority_instance_id=instance,
    )


def _stage(registry: MediaArtifactRegistry, name: str, data: bytes = _PNG_1X1) -> Path:

    staging = registry.staging_root

    staging.mkdir(parents=True, exist_ok=True)

    target = staging / name

    target.parent.mkdir(parents=True, exist_ok=True)

    target.write_bytes(data)

    return target


# ---------------------------------------------------------------------------

# Ingress: name defense before anything else

# ---------------------------------------------------------------------------


async def test_ingest_mints_opaque_instance_scoped_ref(tmp_path: Path) -> None:
    """The happy path: a staged PNG ingests into the OWNER-side store and

    the client receives an opaque ref bound to this owner instance, the

    exact digest, and media metadata."""

    registry = _registry(tmp_path)

    _stage(registry, "photo.png")

    ref = registry.ingest("photo.png")

    assert len(ref.ref) == 32 and ref.ref != "photo.png", "opaque, not the staged name"

    assert ref.instance_id == INSTANCE

    assert ref.mime == "image/png"

    assert ref.byte_size == len(_PNG_1X1)

    assert ref.sha256.startswith("a") or len(ref.sha256) == 64

    # The owner copy lives in the owner-controlled artifact store, named

    # by content — the staged file is no longer authority.

    assert Path(ref.owner_path).is_relative_to(tmp_path / "artifacts")  # noqa: ASYNC240

    assert Path(ref.owner_path).name == f"{ref.sha256}.bin"  # noqa: ASYNC240


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "C:/abs/path.png",
        "/abs/path.png",
        "../escape.png",
        "sub/../../escape.png",
        "a" * 200,
    ],
)
async def test_ingest_rejects_dangerous_names(tmp_path: Path, bad_name: str) -> None:
    """Absolute paths, traversal components, and oversized names die at

    the name gate — client-relative resolution can never happen."""

    registry = _registry(tmp_path)

    _stage(registry, "photo.png")

    with pytest.raises(MediaIngressError, match="invalid_staged_name"):
        registry.ingest(bad_name)


async def test_ingest_rejects_symlink_escape(tmp_path: Path) -> None:
    """A symlink inside staging pointing outside the owner root is

    rejected — aliasing cannot smuggle foreign bytes."""

    registry = _registry(tmp_path)

    staging = _stage(registry, "photo.png")

    outside = tmp_path / "outside.png"

    outside.write_bytes(_PNG_1X1)

    try:
        (staging.parent / "link.png").symlink_to(outside)

    except OSError:
        pytest.skip("symlink creation requires privileges on this platform")

    with pytest.raises(MediaIngressError, match="outside|symlink"):
        registry.ingest("link.png")


async def test_ingest_rejects_unsupported_and_corrupt_media(tmp_path: Path) -> None:
    """The EXISTING validation pipeline gates ingress: non-image bytes

    and empty files die with the pipeline's own codes."""

    registry = _registry(tmp_path)

    _stage(registry, "note.txt", data=b"just text, no magic bytes")

    with pytest.raises(MediaIngressError, match="unsupported_format"):
        registry.ingest("note.txt")

    _stage(registry, "empty.png", data=b"")

    with pytest.raises(MediaIngressError, match="empty_file|not_regular|not_found"):
        registry.ingest("empty.png")


async def test_ingest_is_immune_to_staged_file_mutation_after_ingest(tmp_path: Path) -> None:
    """The owner copy is content-addressed from the bytes the owner read:

    mutating or deleting the staged file afterwards changes nothing —

    resolution still yields the exact validated bytes."""

    registry = _registry(tmp_path)

    staged = _stage(registry, "photo.png")

    ref = registry.ingest("photo.png")

    staged.write_bytes(_PNG_1X1 + b"tampered")

    staged.unlink()

    resolved = registry.resolve(ref.ref)

    assert resolved.sha256 == ref.sha256

    assert Path(resolved.owner_path).read_bytes() == _PNG_1X1  # noqa: ASYNC240


# ---------------------------------------------------------------------------

# Resolution: digest binding, substitution defense, instance scope

# ---------------------------------------------------------------------------


async def test_resolve_reverifies_owner_copy_digest(tmp_path: Path) -> None:
    """Substitution defense: if the owner-side copy is corrupted, resolve

    fails with digest_mismatch rather than handing tampered bytes to a

    mutation."""

    registry = _registry(tmp_path)

    _stage(registry, "photo.png")

    ref = registry.ingest("photo.png")

    Path(ref.owner_path).write_bytes(_PNG_1X1 + b"corrupted")  # noqa: ASYNC240

    with pytest.raises(MediaIngressError, match="digest_mismatch"):
        registry.resolve(ref.ref)


async def test_resolve_binds_expected_digest_from_confirmation(tmp_path: Path) -> None:
    """The confirm-time resolution can require the digest the preview

    showed — binding the exact artifact into the confirmed mutation."""

    registry = _registry(tmp_path)

    _stage(registry, "photo.png")

    ref = registry.ingest("photo.png")

    resolved = registry.resolve(ref.ref, expected_digest=ref.sha256)

    assert resolved.sha256 == ref.sha256

    with pytest.raises(MediaIngressError, match="digest_mismatch"):
        registry.resolve(ref.ref, expected_digest="0" * 64)


async def test_refs_die_with_the_owner_instance(tmp_path: Path) -> None:
    """Artifact refs are EPHEMERAL instance state: a successor owner

    (new instance id) never honors the old refs — restart invalidates,

    and refs can never become durable replay credentials."""

    first = _registry(tmp_path)

    _stage(first, "photo.png")

    ref = first.ingest("photo.png")

    assert first.resolve(ref.ref).sha256 == ref.sha256

    # The successor registry is a fresh process-local store: the old ref

    # simply does not exist in it — restart invalidates by construction

    # (the wire layer additionally rejects refs minted under another

    # instance id before the registry is even consulted).

    successor = _registry(tmp_path, instance="n" * 64)

    with pytest.raises(MediaIngressError, match="unknown_artifact"):
        successor.resolve(ref.ref)


# ---------------------------------------------------------------------------

# Ordered all-or-nothing resolution + pinning + retention

# ---------------------------------------------------------------------------


async def test_resolve_ordered_is_all_or_nothing(tmp_path: Path) -> None:
    """Multi-image ordering: every item resolves and re-verifies FIRST;

    one bad item resolves NOTHING (no partial media authority)."""

    registry = _registry(tmp_path)

    _stage(registry, "one.png")

    _stage(registry, "two.png", data=_PNG_1X1 + bytes([0]))  # distinct digest

    good_a = registry.ingest("one.png")

    good_b = registry.ingest("two.png")

    assert good_a.sha256 != good_b.sha256, "distinct content-addressed copies"

    _stage(registry, "bad.png", data=_PNG_1X1 + b"")

    bad = registry.ingest("bad.png")

    Path(bad.owner_path).write_bytes(b"corrupted owner copy")  # noqa: ASYNC240

    resolved = registry.resolve_ordered([good_a.ref, good_b.ref])

    assert [r.ref for r in resolved] == [good_a.ref, good_b.ref], "order preserved"

    with pytest.raises(MediaIngressError, match="digest_mismatch"):
        registry.resolve_ordered([good_a.ref, bad.ref])


async def test_pinning_blocks_retention_deletion(tmp_path: Path) -> None:
    """Admitted-mutation pinning: a pinned artifact cannot be deleted by

    cleanup/retention; unpinned artifacts can. Disconnect/cleanup can

    never delete live media."""

    registry = _registry(tmp_path)

    _stage(registry, "pinned.png")

    _stage(registry, "free.png")

    pinned = registry.ingest("pinned.png")

    free = registry.ingest("free.png")

    registry.pin(pinned.ref, "request-1")

    registry.cleanup_unpinned()

    assert Path(pinned.owner_path).exists()  # noqa: ASYNC240

    assert registry.resolve(pinned.ref).sha256 == pinned.sha256

    with pytest.raises(MediaIngressError, match="unknown_artifact"):
        registry.resolve(free.ref)

    # Unpin at the safe terminal boundary: cleanup may now reclaim it.

    registry.unpin(pinned.ref, "request-1")

    registry.cleanup_unpinned()

    with pytest.raises(MediaIngressError, match="unknown_artifact"):
        registry.resolve(pinned.ref)


async def test_registry_is_bounded(tmp_path: Path) -> None:
    """The registry holds a bounded number of live artifacts; overflow

    refuses new ingress (with a cleanup hint), never evicts silently."""

    small = MediaArtifactRegistry(
        staging_root=tmp_path / "staging",
        artifact_root=tmp_path / "artifacts",
        authority_instance_id=INSTANCE,
        max_artifacts=2,
    )

    for name in ("a.png", "b.png", "c.png"):
        _stage(small, name)

    assert small.ingest("a.png").ref

    assert small.ingest("b.png").ref

    with pytest.raises(MediaIngressError, match="registry_full"):
        small.ingest("c.png")
