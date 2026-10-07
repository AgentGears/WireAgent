"""M7 Layer 6 — owner-side artifact ingress and the opaque media
referent model (frozen Layer-6 boundary).

The client can never turn an arbitrary filesystem string into a media
mutation by placing it in an IPC payload. Bytes enter through the
OWNER-controlled staging root; acquisition is BOUNDED and
race-resistant (the staged file is streamed once into an owner-side
temporary copy that then becomes the immutable object of validation);
the EXISTING media validation pipeline (upload roots, magic-byte MIME,
size, dimensions, SHA-256, EXIF) gates ingress BEFORE any preview/token
authority; the validated bytes are published atomically into an
owner-side content-addressed store; and the minted artifact_ref is an
OPAQUE, instance-scoped, ephemeral handle — never M5/M6 authority and
never a durable replay credential (a successor owner knows nothing of
it).

Both roots are proven REAL directories beneath the canonical authority
domain (no symlink/reparse roots), and every staged-name component is
walked for symlinks before the file is opened. Pinned artifacts
(admitted mutation work) cannot be deleted by retention/cleanup or
released through IPC until the mutation's safe terminal boundary
unpins them.
"""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from webwire.safety.attachment import _MAX_FILE_BYTES, validate_media_file

__all__ = [
    "MediaArtifactRegistry",
    "MediaArtifactRef",
    "MediaIngressError",
    "MEDIA_MAX_STAGED_NAME_CHARS",
    "MEDIA_DEFAULT_MAX_ARTIFACTS",
]

logger = logging.getLogger(__name__)


def _close_win_handle(handle: Any) -> None:
    """Close a Win32 handle via runtime-resolved windll (ctypes.windll is
    Windows-only in typeshed — the F-25 portability pattern)."""
    import ctypes

    windll = getattr(ctypes, "windll", None)
    if windll is not None:
        windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


MEDIA_MAX_STAGED_NAME_CHARS = 128
MEDIA_DEFAULT_MAX_ARTIFACTS = 256
_READ_CHUNK_BYTES = 1 << 16


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

    Two locations under the CANONICAL AUTHORITY DOMAIN: the staging root
    (where clients place bytes; the owner resolves names strictly inside
    it) and the artifact root (content-addressed owner copies, named
    ``<sha256>.bin``). Both roots are proven real, non-symlink
    directories beneath the domain at construction — a pre-existing
    symlinked root (which would let cleanup unlink foreign files) is
    rejected outright."""

    def __init__(
        self,
        *,
        staging_root: Path,
        artifact_root: Path,
        authority_instance_id: str,
        authority_domain: Optional[Path] = None,
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
        # F-82: the registry is confined to the canonical authority
        # domain. Roots may be created if absent, but whatever exists at
        # those paths must be REAL directories whose resolved locations
        # stay beneath the resolved domain — a symlink/reparse root is a
        # confinement breach (cleanup could unlink foreign files).
        self._authority_domain = (
            Path(authority_domain) if authority_domain is not None else self._staging_root.parent
        )
        domain_resolved = self._authority_domain.resolve()
        domain_resolved.mkdir(parents=True, exist_ok=True)
        for root in (self._staging_root, self._artifact_root):
            self._establish_root(root, domain_resolved)

    def _establish_root(self, root: Path, domain_resolved: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink():
            raise MediaIngressError(
                "root_symlink_rejected",
                f"media root {root} is a symlink — refusing to operate outside the authority domain",
            )
        resolved = root.resolve()
        if not resolved.is_relative_to(domain_resolved):
            raise MediaIngressError(
                "root_outside_authority_domain",
                f"media root {root} resolves outside the canonical authority domain",
            )
        # Every path component between the domain and the root must
        # itself be real (a symlinked PARENT component is the same
        # breach one level up).
        probe = domain_resolved
        relative = resolved.relative_to(domain_resolved)
        for part in relative.parts:
            probe = probe / part
            if probe.is_symlink():
                raise MediaIngressError(
                    "root_symlink_rejected",
                    f"media root path component {probe} is a symlink",
                )

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

    def _resolve_staged_path(self, staged_name: str) -> Path:
        """Walk every staged-name COMPONENT for symlinks (an aliasing
        directory that resolves back inside staging is still an alias),
        then prove the final path is a regular file inside the root."""
        root = self._staging_root.resolve()
        probe = self._staging_root
        for part in staged_name.split("/"):
            probe = probe / part
            if probe.is_symlink():
                raise MediaIngressError(
                    "symlink_rejected", f"symlinks are not allowed: {staged_name}"
                )
        resolved = probe.resolve()
        if not resolved.is_relative_to(root):
            raise MediaIngressError(
                "staged_outside_root", f"staged name resolves outside the staging root: {staged_name}"
            )
        if not resolved.is_file():
            raise MediaIngressError(
                "not_regular_file", f"staged name is not a regular file: {staged_name}"
            )
        return resolved

    def _openat(self, dir_fd: int, name: str, flags: int) -> int:
        """F-88 seam: open one path component relative to a directory
        descriptor. Tests wrap this to inject the check-to-open race; the
        production behavior is the no-follow openat itself."""
        return os.open(name, flags, dir_fd=dir_fd)

    def _open_staged_confined(self, staged_name: str) -> int:
        """F-88: the final open IS part of the confinement decision.

        POSIX: a dir_fd walk from the staging root opens every component
        with O_NOFOLLOW — a component swapped for a symlink/reparse
        alias between check and open fails the OPEN itself (ELOOP), so
        the bytes acquired can only be bytes inside the staging tree.

        Windows: the final component is probed with
        FILE_FLAG_OPEN_REPARSE_POINT (opening the LINK itself when the
        component is a reparse point) and the handle's own attributes are
        inspected — a reparse/symlink alias is refused at the handle
        level; intermediate components are checked as best the portable
        surface allows (full platform qualification is Layers 7/8).
        Returns a file descriptor for the confined regular file."""
        import stat as _stat

        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if nofollow:
            try:
                # The staging root itself joins the no-follow chain: the
                # root path cannot become the one component outside it.
                dir_fd = os.open(
                    self._staging_root,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow,
                )
            except OSError as exc:
                raise MediaIngressError(
                    "acquisition_failed", f"the staging root could not be opened: {exc!r}"
                ) from exc
            try:
                parts = staged_name.split("/")
                for index, part in enumerate(parts):
                    last = index == len(parts) - 1
                    flags = os.O_RDONLY | nofollow | (0 if last else getattr(os, "O_DIRECTORY", 0))
                    fd = self._openat(dir_fd, part, flags)
                    os.close(dir_fd)
                    dir_fd = fd
                info = os.fstat(dir_fd)
                if not _stat.S_ISREG(info.st_mode):
                    os.close(dir_fd)
                    raise MediaIngressError(
                        "not_regular_file", f"staged name is not a regular file: {staged_name}"
                    )
                return dir_fd
            except MediaIngressError:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                raise
            except OSError as exc:
                try:
                    os.close(dir_fd)
                except OSError:
                    pass
                if exc.errno in (errno.ELOOP, errno.EMLINK):
                    raise MediaIngressError(
                        "symlink_rejected",
                        f"symlinks are not allowed: {staged_name} (refused at open)",
                    ) from exc
                if isinstance(exc, FileNotFoundError):
                    raise MediaIngressError(
                        "not_regular_file", f"staged name is not a regular file: {staged_name}"
                    ) from exc
                raise MediaIngressError(
                    "acquisition_failed", f"the staged file could not be opened: {exc!r}"
                ) from exc
        return self._win_open_final(staged_name)

    def _win_open_final(self, staged_name: str) -> int:
        """F-90: ONE Windows handle is both the confinement decision and
        the acquisition source. The final component is opened with
        FILE_FLAG_OPEN_REPARSE_POINT (no-follow: a reparse/symlink
        component yields a handle to the LINK itself), the handle's own
        attributes are inspected — fail CLOSED if identity cannot be
        established — the handle's FINAL resolved path must be beneath
        the staging root and a regular file, and THIS SAME handle is
        bridged into the bounded reader. The pathname is never reopened.
        Intermediate components stay path-checked; their substitution is
        caught by the final-path containment proof on this handle."""

        handle = self._win_create_validated(staged_name)
        try:
            final = self._win_final_path(handle)
            root = os.path.normcase(str(self._staging_root.resolve()))
            if not os.path.normcase(final).startswith(root + os.sep) and os.path.normcase(
                final
            ) != root:
                raise MediaIngressError(
                    "staged_outside_root",
                    f"staged name resolves outside the staging root: {staged_name}",
                )
            msvcrt = __import__("importlib", fromlist=["import_module"]).import_module("msvcrt")
            fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | getattr(os, "O_BINARY", 0))
        except MediaIngressError:
            _close_win_handle(handle)
            raise
        except OSError as exc:
            _close_win_handle(handle)
            raise MediaIngressError(
                "acquisition_failed", f"the staged file could not be opened: {exc!r}"
            ) from exc
        return fd

    def _win_create_validated(self, staged_name: str) -> Any:
        """Open the final component no-follow and validate the HANDLE's
        own attributes: reparse/symlink → symlink_rejected; directory →
        not_regular_file; open/inspection failure → acquisition_failed
        (fail CLOSED — 'unable to prove' is never treated as safe)."""
        import ctypes
        import ctypes.wintypes as wt

        windll = getattr(ctypes, "windll", None)
        if windll is None:  # pragma: no cover - non-Windows
            raise MediaIngressError("acquisition_failed", "the Windows open path is unavailable")
        kernel32 = windll.kernel32
        FILE_FLAG_OPEN_REPARSE_POINT = 0x200000
        FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
        GENERIC_READ = 0x80000000
        OPEN_EXISTING = 3
        kernel32.CreateFileW.restype = ctypes.c_void_p
        handle = kernel32.CreateFileW(
            str(self._staging_root / staged_name),
            GENERIC_READ,
            0,
            None,
            OPEN_EXISTING,
            FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        if not handle or handle == -1:
            raise MediaIngressError(
                "acquisition_failed",
                f"the staged file could not be opened (no-follow open refused): {staged_name}",
            )

        class _BHFI(ctypes.Structure):
            _fields_ = [
                ("dwFileAttributes", wt.DWORD),
                ("ftCreationTime", wt.FILETIME),
                ("ftLastAccessTime", wt.FILETIME),
                ("ftLastWriteTime", wt.FILETIME),
                ("dwVolumeSerialNumber", wt.DWORD),
                ("nFileSizeHigh", wt.DWORD),
                ("nFileSizeLow", wt.DWORD),
                ("nNumberOfLinks", wt.DWORD),
                ("nFileIndexHigh", wt.DWORD),
                ("nFileIndexLow", wt.DWORD),
            ]

        info = _BHFI()
        if not kernel32.GetFileInformationByHandle(ctypes.c_void_p(handle), ctypes.byref(info)):
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise MediaIngressError(
                "acquisition_failed",
                f"the staged file's identity could not be established: {staged_name}",
            )
        if info.dwFileAttributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise MediaIngressError(
                "symlink_rejected",
                f"symlinks/reparse points are not allowed: {staged_name} (refused at open)",
            )
        if info.dwFileAttributes & 0x10:  # FILE_ATTRIBUTE_DIRECTORY
            kernel32.CloseHandle(ctypes.c_void_p(handle))
            raise MediaIngressError(
                "not_regular_file", f"staged name is not a regular file: {staged_name}"
            )
        return handle

    @staticmethod
    def _win_final_path(handle: Any) -> str:
        """GetFinalPathNameByHandleW on the SAME validated handle (the
        containment proof for every intermediate component too)."""
        import ctypes

        windll = getattr(ctypes, "windll", None)
        if windll is None:  # pragma: no cover - non-Windows
            raise MediaIngressError("acquisition_failed", "the Windows path is unavailable")
        kernel32 = windll.kernel32
        kernel32.GetFinalPathNameByHandleW.restype = ctypes.c_uint32
        kernel32.GetFinalPathNameByHandleW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
        ]
        buffer = ctypes.create_unicode_buffer(32768)
        length = kernel32.GetFinalPathNameByHandleW(
            ctypes.c_void_p(handle), buffer, 32768, 0
        )
        if not length or length >= 32768:
            raise MediaIngressError(
                "acquisition_failed", "the staged file's final path could not be established"
            )
        final = buffer.value
        if final.startswith("\\\\?\\"):
            final = final[4:]
        return final

    def _acquire_bounded_copy(self, source_fd: int) -> Path:
        """F-83/F-88: BOUNDED, race-resistant acquisition FROM the
        confined descriptor. The staged file is streamed ONCE into an
        owner-controlled temporary file inside the artifact root, reading
        at most the validator's cap plus one byte — a post-open grow
        cannot force an unbounded allocation, and a mid-read deletion or
        swap affects only the read, never the system. The temporary copy
        is the immutable object everything later validates, hashes, and
        publishes."""
        temp_path = self._artifact_root / f".ingest-{secrets.token_hex(8)}.tmp"
        try:
            with os.fdopen(source_fd, "rb") as src, open(temp_path, "wb") as dst:
                total = 0
                while True:
                    chunk = src.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        raise MediaIngressError(
                            "media_validation_failed",
                            f"File is over {_MAX_FILE_BYTES} bytes (bounded acquisition refused)",
                        )
                    dst.write(chunk)
        except MediaIngressError:
            temp_path.unlink(missing_ok=True)
            raise
        except OSError as exc:
            temp_path.unlink(missing_ok=True)
            raise MediaIngressError(
                "acquisition_failed", f"the staged file could not be read: {exc!r}"
            ) from exc
        return temp_path

    def ingest(self, staged_name: str) -> MediaArtifactRef:
        """Validate and ingest one staged artifact; returns the minted
        opaque ref. Any failure leaves NO new registry entry and NO
        published artifact."""
        self._check_staged_name(staged_name)
        self._resolve_staged_path(staged_name)  # F-82 name/root/alias gates
        with self._lock:
            if len(self._entries) >= self._max_artifacts:
                raise MediaIngressError(
                    "registry_full",
                    f"the artifact registry holds its maximum of {self._max_artifacts} "
                    "artifacts; release unreferenced artifacts or run cleanup",
                )
            # F-88: the open is part of the confinement decision — the
            # held descriptor (not a re-resolved path) feeds acquisition.
            confined_fd = self._open_staged_confined(staged_name)
            temp_path = self._acquire_bounded_copy(confined_fd)
            try:
                # The EXISTING validation pipeline (upload roots, MIME,
                # size, dimensions, SHA-256, EXIF) runs on the immutable
                # OWNER COPY — the bytes validated are exactly the bytes
                # later referenced.
                try:
                    attachment = validate_media_file(temp_path, upload_roots=[self._artifact_root.resolve()])
                except Exception as exc:  # MediaValidationError from the pipeline
                    raise MediaIngressError("media_validation_failed", str(exc)) from exc
                digest = hashlib.sha256()
                with open(temp_path, "rb") as f:
                    for chunk in iter(lambda: f.read(_READ_CHUNK_BYTES), b""):
                        digest.update(chunk)
                if digest.hexdigest() != attachment.sha256:
                    raise MediaIngressError(
                        "digest_mismatch", "the acquired copy failed its digest cross-check"
                    )
                owner_path = self._artifact_root / f"{attachment.sha256}.bin"
                # Atomic publish: the content-addressed object appears
                # whole or not at all; an existing object with the same
                # name carries the same bytes by construction.
                os.replace(temp_path, owner_path)
            except BaseException:
                temp_path.unlink(missing_ok=True)
                raise
            artifact = MediaArtifactRef(
                ref=secrets.token_hex(16),
                instance_id=self._instance_id,
                sha256=attachment.sha256,
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

    @staticmethod
    def _bounded_digest(owner_path: Path) -> str:
        """F-87: BOUNDED, STABLE owner-copy revalidation. Streams at most
        the validator cap plus one byte — an oversize/corrupt copy is
        rejected without an unbounded allocation — and converts every
        open/read failure to a stable ingress code (a vanished or
        unreadable owner copy is ``artifact_deleted``/``artifact_unreadable``,
        never a raw OSError escaping to a generic internal)."""
        digest = hashlib.sha256()
        total = 0
        try:
            with open(owner_path, "rb") as f:
                while True:
                    chunk = f.read(_READ_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _MAX_FILE_BYTES:
                        raise MediaIngressError(
                            "artifact_oversize",
                            "the owner-side artifact copy exceeds the bounded size",
                        )
                    digest.update(chunk)
        except FileNotFoundError as exc:
            raise MediaIngressError(
                "artifact_deleted", "the owner-side artifact copy no longer exists"
            ) from exc
        except OSError as exc:
            raise MediaIngressError(
                "artifact_unreadable", f"the owner-side artifact copy could not be read: {exc!r}"
            ) from exc
        return digest.hexdigest()

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
            current = self._bounded_digest(Path(artifact.owner_path))
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

    # -- release, pinning, retention -----------------------------------------

    def release(self, ref_token: str) -> None:
        """F-84/F-89: explicit client-driven reclamation with TRUTHFUL
        deletion semantics. A PINNED artifact (live admitted mutation
        work) cannot be released; a pending confirmation token's ref
        stays valid until released or cleaned — never silently evicted.
        The owner file is deleted FIRST and the registry entry is removed
        only on success: if deletion fails the release raises
        ``artifact_release_failed`` and BOTH the reference and the file
        remain — the client is never told "released" while media stays
        on disk."""
        with self._lock:
            entry = self._entries.get(ref_token)
            if entry is None:
                raise MediaIngressError(
                    "unknown_artifact",
                    "unknown, expired, or restarted artifact reference",
                )
            if entry.pins:
                raise MediaIngressError(
                    "artifact_pinned",
                    "the artifact is pinned by live admitted work and cannot be released",
                )
            if any(
                other.artifact.sha256 == entry.artifact.sha256
                for token, other in self._entries.items()
                if token != ref_token
            ):
                # Another live entry shares the content-addressed file:
                # removing this reference is complete reclamation.
                del self._entries[ref_token]
                return
            target = Path(entry.artifact.owner_path)
            try:
                target.unlink()
            except FileNotFoundError:
                # F-91: the content-addressed file is ALREADY absent —
                # reclamation is complete at the storage layer; keeping
                # the entry would strand a permanently unreleasable
                # reference (resolve() already reports artifact_deleted).
                del self._entries[ref_token]
                return
            except OSError as exc:
                raise MediaIngressError(
                    "artifact_release_failed",
                    f"the artifact reference could not be deleted from storage: {exc!r}",
                ) from exc
            del self._entries[ref_token]

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
        """BEST-EFFORT retention (F-89 truth): remove unpinned registry
        entries and delete owner copies no surviving entry uses. Returns
        the number of entries removed; pinned artifacts always survive.
        File deletion here is DIAGNOSTIC-BEST-EFFORT — an unlink failure
        is logged (residue may remain after shutdown) and is NOT a
        guaranteed-removal claim. Guaranteed storage reclamation is the
        explicit media_release contract, which fails honestly instead."""
        removed = 0
        residue = 0
        with self._lock:
            survivors: dict[str, _Entry] = {}
            for token, entry in self._entries.items():
                if entry.pins:
                    survivors[token] = entry
                    continue
                removed += 1
            self._entries = survivors
            referenced = {entry.artifact.sha256 for entry in survivors.values()}
            for path in self._artifact_root.glob("*.bin"):
                if path.stem not in referenced:
                    try:
                        path.unlink()
                    except OSError:
                        residue += 1
        if residue:
            logger.warning(
                "media retention left %d artifact file(s) on disk (best-effort cleanup; "
                "unlinks failed)",
                residue,
            )
        return removed
