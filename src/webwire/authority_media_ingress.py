"""M7 Layer 6 — owner-side artifact ingress and the opaque media
referent model (frozen Layer-6 boundary).

The client can never turn an arbitrary filesystem string into a media
mutation by placing it in an IPC payload. Bytes enter through the
OWNER-controlled staging root; the EXISTING media validation pipeline
(upload roots, regular-file/no-symlink, magic-byte MIME, size, dimensions,
SHA-256, EXIF) gates ingress BEFORE any preview/token authority; the
exact validated bytes are copied into an owner-side content-addressed
store; and the minted artifact_ref is an OPAQUE, instance-scoped,
ephemeral handle — never M5/M6 authority and never a durable replay
credential (a successor owner knows nothing of it).

Pinned artifacts (admitted mutation work) cannot be deleted by
retention/cleanup until the mutation's safe terminal boundary unpins
them.
"""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from webwire.safety.attachment import validate_media_file

__all__ = [
    "MediaArtifactRegistry",
    "MediaArtifactRef",
    "MediaIngressError",
    "MEDIA_MAX_STAGED_NAME_CHARS",
    "MEDIA_DEFAULT_MAX_ARTIFACTS",
]

MEDIA_MAX_STAGED_NAME_CHARS = 128
MEDIA_DEFAULT_MAX_ARTIFACTS = 256


class MediaIngressError(RuntimeError):
    """One operational exception type; ``code`` is the stable wire code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class MediaArtifactRef:
    """The opaque client-facing handle and the owner's binding of it to
    exact bytes. The ``ref`` token carries no path or authority meaning;
    everything else is owner-side truth."""

    ref: str
    instance_id: str
    sha256: str
    mime: str
    byte_size: int
    owner_path: str
    width: Optional[int] = None
    height: Optional[int] = None
    ingested_at: float = 0.0

    def to_wire_dict(self) -> dict[str, Any]:
        """The bounded preview-grade metadata a client (and its human)
        sees when the artifact is ingested — never the staging name."""
        return {
            "artifact_ref": self.ref,
            "sha256": self.sha256,
            "mime": self.mime,
            "byte_size": self.byte_size,
            "width": self.width,
            "height": self.height,
        }


@dataclass
class _Entry:
    artifact: MediaArtifactRef
    pins: set[str] = field(default_factory=set)


class MediaArtifactRegistry:
    """Process-local, instance-scoped artifact state for ONE owner.

    The registry owns two locations under the canonical authority domain:
    the staging root (where clients place bytes; the owner resolves
    names strictly inside it) and the artifact root (content-addressed
    owner copies, named ``<sha256>.bin``)."""

    def __init__(
        self,
        *,
        staging_root: Path,
        artifact_root: Path,
        authority_instance_id: str,
        max_artifacts: int = MEDIA_DEFAULT_MAX_ARTIFACTS,
    ) -> None:
        if max_artifacts < 1:
            raise ValueError("max_artifacts must be >= 1")
        self._staging_root = Path(staging_root)
        self._artifact_root = Path(artifact_root)
        self._instance_id = authority_instance_id
        self._max_artifacts = max_artifacts
        self._lock = threading.RLock()
        self._entries: dict[str, _Entry] = {}
        self._staging_root.mkdir(parents=True, exist_ok=True)
        self._artifact_root.mkdir(parents=True, exist_ok=True)

    @property
    def staging_root(self) -> Path:
        return self._staging_root

    @property
    def artifact_root(self) -> Path:
        return self._artifact_root

    @property
    def artifact_count(self) -> int:
        with self._lock:
            return len(self._entries)

    # -- ingress -----------------------------------------------------------

    def _check_staged_name(self, staged_name: Any) -> None:
        """The name gate: absolute paths, traversal, backslashes, drive
        letters, and oversized names die HERE — a client-relative path
        can never be resolved against anything."""
        if not isinstance(staged_name, str) or not staged_name:
            raise MediaIngressError("invalid_staged_name", "staged name must be a non-empty string")
        if len(staged_name) > MEDIA_MAX_STAGED_NAME_CHARS:
            raise MediaIngressError("invalid_staged_name", "staged name exceeds the length bound")
        if "\\" in staged_name:
            raise MediaIngressError("invalid_staged_name", "backslash separators are not staging names")
        if staged_name.startswith("/") or (len(staged_name) > 1 and staged_name[1] == ":"):
            raise MediaIngressError("invalid_staged_name", "absolute paths are not staging names")
        parts = staged_name.split("/")
        if any(part in ("", ".", "..") for part in parts):
            raise MediaIngressError(
                "invalid_staged_name", "traversal or empty components are not staging names"
            )

    def ingest(self, staged_name: str) -> MediaArtifactRef:
        """Validate and ingest one staged artifact; returns the minted
        opaque ref. Any failure leaves NO new registry entry."""
        self._check_staged_name(staged_name)
        candidate = self._staging_root / staged_name
        # Symlinks are rejected before resolution — an alias cannot
        # smuggle foreign bytes past the staging boundary.
        if candidate.is_symlink():
            raise MediaIngressError("symlink_rejected", f"symlinks are not allowed: {staged_name}")
        root = self._staging_root.resolve()
        resolved = candidate.resolve()
        if not resolved.is_relative_to(root):
            raise MediaIngressError(
                "staged_outside_root", f"staged name resolves outside the staging root: {staged_name}"
            )
        # The EXISTING validation pipeline (upload roots, MIME, size,
        # dimensions, SHA-256, EXIF) gates everything before authority.
        try:
            attachment = validate_media_file(resolved, upload_roots=[root])
        except Exception as exc:  # MediaValidationError from the pipeline
            raise MediaIngressError(
                "media_validation_failed", str(exc)
            ) from exc
        # Read the exact bytes ONCE (bounded by the validated size) and
        # prove they still hash to the validated digest — the owner copy
        # is then built from exactly these bytes, immune to staged-file
        # mutation afterwards.
        data = resolved.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != attachment.sha256:
            raise MediaIngressError(
                "digest_mismatch", "the staged file changed during ingestion"
            )
        with self._lock:
            if len(self._entries) >= self._max_artifacts:
                raise MediaIngressError(
                    "registry_full",
                    f"the artifact registry holds its maximum of {self._max_artifacts} "
                    "artifacts; run cleanup or close work",
                )
            owner_path = self._artifact_root / f"{digest}.bin"
            if not owner_path.exists():
                owner_path.write_bytes(data)
            # Content-addressed guarantee: the stored copy must hash to
            # the digest it is named by.
            readback = hashlib.sha256(owner_path.read_bytes()).hexdigest()
            if readback != digest:
                raise MediaIngressError(
                    "digest_mismatch", "the owner-side artifact copy failed its read-back digest check"
                )
            artifact = MediaArtifactRef(
                ref=secrets.token_hex(16),
                instance_id=self._instance_id,
                sha256=digest,
                mime=attachment.mime,
                byte_size=attachment.byte_size,
                owner_path=str(owner_path),
                width=attachment.width,
                height=attachment.height,
                ingested_at=time.time(),
            )
            self._entries[artifact.ref] = _Entry(artifact=artifact)
            return artifact

    # -- resolution ---------------------------------------------------------

    def resolve(
        self,
        ref_token: Any,
        *,
        expected_digest: Optional[str] = None,
    ) -> MediaArtifactRef:
        """Resolve an opaque ref to its owner-side artifact, RE-VERIFYING
        the owner copy's digest first (substitution/corruption defense).
        ``expected_digest`` binds the exact artifact the preview showed."""
        with self._lock:
            entry = self._entries.get(ref_token) if isinstance(ref_token, str) else None
            if entry is None:
                raise MediaIngressError(
                    "unknown_artifact",
                    "unknown, expired, or restarted artifact reference",
                )
            artifact = entry.artifact
            owner_path = Path(artifact.owner_path)
            if not owner_path.exists():
                raise MediaIngressError(
                    "artifact_deleted", "the owner-side artifact copy no longer exists"
                )
            current = hashlib.sha256(owner_path.read_bytes()).hexdigest()
            if current != artifact.sha256:
                raise MediaIngressError(
                    "digest_mismatch",
                    "the owner-side artifact copy no longer matches its bound digest",
                )
            if expected_digest is not None and expected_digest != artifact.sha256:
                raise MediaIngressError(
                    "digest_mismatch",
                    "the artifact reference does not carry the digest the preview bound",
                )
            return artifact

    def resolve_ordered(self, ref_tokens: list[str]) -> list[MediaArtifactRef]:
        """Ordered ALL-OR-NOTHING resolution: every item resolves and
        re-verifies before anything is returned — a bad item N resolves
        nothing (no partial media authority for multi-image work)."""
        return [self.resolve(token) for token in ref_tokens]

    # -- pinning + retention -------------------------------------------------

    def pin(self, ref_token: str, pin_id: str) -> None:
        """Pin an artifact for the duration of admitted mutation work —
        retention/cleanup cannot delete pinned bytes."""
        with self._lock:
            entry = self._entries.get(ref_token)
            if entry is None:
                raise MediaIngressError("unknown_artifact", "cannot pin an unknown artifact reference")
            entry.pins.add(pin_id)

    def unpin(self, ref_token: str, pin_id: str) -> None:
        with self._lock:
            entry = self._entries.get(ref_token)
            if entry is not None:
                entry.pins.discard(pin_id)

    def cleanup_unpinned(self) -> int:
        """Retention: remove unpinned registry entries and delete owner
        copies no surviving entry uses. Returns the number of entries
        removed. Pinned artifacts always survive."""
        removed = 0
        with self._lock:
            survivors: dict[str, _Entry] = {}
            for token, entry in self._entries.items():
                if entry.pins:
                    survivors[token] = entry
                    continue
                removed += 1
            self._entries = survivors
            # Delete content-addressed files nobody references anymore.
            referenced = {entry.artifact.sha256 for entry in survivors.values()}
            for path in self._artifact_root.glob("*.bin"):
                if path.stem not in referenced:
                    try:
                        path.unlink()
                    except OSError:
                        pass
        return removed
