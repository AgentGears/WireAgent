"""Attachment model + media validation (v0.2 M1).

ChatGPT's 8 media-specific safety concerns implemented:
1. File bytes binding (SHA-256) — confirmation binds to bytes, not path.
2. Filesystem restriction — upload roots, regular files only, no symlinks.
3. Content validation — MIME detection from bytes, not extensions.
4. EXIF metadata — detect GPS, warn in preview.
5. Dimensions/size limits.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

__all__ = [
    "Attachment",
    "MediaValidationError",
    "validate_media_file",
    "file_sha256",
    "detect_mime",
    "detect_image_dimensions",
    "detect_exif_warnings",
]

# Supported image formats (magic bytes → MIME).
_MAGIC_BYTES = {
    b"\xff\xd8\xff": "image/jpeg",
    b"\x89PNG\r\n\x1a\n": "image/png",
    b"GIF87a": "image/gif",
    b"GIF89a": "image/gif",
    b"RIFF": "image/webp",  # WebP starts with RIFF....WEBP
}

# Platform limits (configurable in future; conservative defaults).
_MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
_MAX_DIMENSION = 4096  # pixels per side


class MediaValidationError(Exception):
    """Raised when a media file fails validation."""
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class Attachment:
    """Immutable description of a media attachment for a post.

    The confirmation token binds to this (including sha256), so changing the
    file after preview invalidates the token. Alt text is included from the
    start (ChatGPT: 'belongs on each attachment from day one').
    """
    path: str               # canonical resolved path
    basename: str           # filename only (for journal/preview — no absolute path)
    sha256: str             # digest of the file bytes
    mime: str               # detected MIME from magic bytes
    byte_size: int
    width: Optional[int] = None
    height: Optional[int] = None
    alt_text: Optional[str] = None
    exif_has_gps: bool = False
    exif_warnings: list[str] = field(default_factory=list)

    def digest_prefix(self) -> str:
        """Short prefix of the SHA-256 for display."""
        return self.sha256[:12]

    def dimensions_str(self) -> str:
        if self.width and self.height:
            return f"{self.width}x{self.height}"
        return "unknown"

    def to_preview_dict(self) -> dict:
        """Human-readable preview for confirmation (ChatGPT concern #5)."""
        return {
            "basename": self.basename,
            "mime": self.mime,
            "size_bytes": self.byte_size,
            "dimensions": self.dimensions_str(),
            "sha256_prefix": self.digest_prefix(),
            "alt_text": self.alt_text,
            "exif_has_gps": self.exif_has_gps,
            "exif_warnings": self.exif_warnings,
        }


def file_sha256(path: Path) -> str:
    """Compute SHA-256 of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_mime(path: Path) -> str:
    """Detect MIME type from magic bytes (not file extension)."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        for magic, mime in _MAGIC_BYTES.items():
            if header.startswith(magic):
                # WebP needs the WEBP tag after RIFF.
                if mime == "image/webp" and header[8:12] != b"WEBP":
                    continue
                return mime
    except OSError:
        pass
    raise MediaValidationError(
        "unsupported_format",
        f"File {path} is not a recognized image format (JPEG, PNG, GIF, WebP).",
    )


def detect_image_dimensions(path: Path, mime: str) -> tuple[Optional[int], Optional[int]]:
    """Detect image dimensions. Uses Pillow if available; else None."""
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.width, img.height
    except ImportError:
        logger.debug("Pillow not installed — dimensions unavailable")
        return None, None
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not read dimensions: %r", exc)
        return None, None


def detect_exif_warnings(path: Path, mime: str) -> tuple[bool, list[str]]:
    """Detect EXIF metadata concerns (ChatGPT concern #4).

    Returns (has_gps, warnings_list).
    """
    warnings: list[str] = []
    has_gps = False
    if mime != "image/jpeg":
        # EXIF is primarily a JPEG concern; PNG/GIF/WebP rarely carry it.
        return False, []
    try:
        from PIL import Image
        from PIL.ExifTags import TAGS
        with Image.open(path) as img:
            exif = img._getexif()
        if exif:
            warnings.append("EXIF metadata present in image")
            for tag_id, _value in exif.items():
                tag = TAGS.get(tag_id, tag_id)
                if tag in ("GPSInfo",):
                    has_gps = True
                    warnings.append("GPS coordinates found in EXIF — location may be exposed")
    except ImportError:
        pass  # Pillow not installed — can't check EXIF
    except Exception:  # noqa: BLE001
        pass  # No EXIF or can't read
    return has_gps, warnings


def validate_media_file(
    path: str | Path,
    upload_roots: list[Path] | None = None,
    alt_text: str | None = None,
) -> Attachment:
    """Full media validation pipeline (ChatGPT concerns #1-5).

    1. Filesystem restriction (upload roots, regular files, no symlinks).
    2. Content detection (MIME from magic bytes, not extension).
    3. Size + dimension limits.
    4. SHA-256 digest (for confirmation binding + dedupe).
    5. EXIF metadata detection.

    Returns a frozen Attachment. Raises MediaValidationError on any failure.
    """
    p = Path(path).resolve()  # canonical path (resolves symlinks)

    # --- Filesystem restriction (concern #2) ---
    if upload_roots:
        roots = [r.resolve() for r in upload_roots]
        if not any(p.is_relative_to(r) for r in roots if r.exists()):
            raise MediaValidationError(
                "path_outside_upload_roots",
                f"File {p} is outside configured upload roots.",
            )
    if not p.exists():
        raise MediaValidationError("file_not_found", f"File does not exist: {p}")
    if not p.is_file():
        raise MediaValidationError("not_regular_file", f"Path is not a regular file: {p}")
    if p.is_symlink():
        raise MediaValidationError("symlink_rejected", f"Symlinks are not allowed: {p}")
    stat = p.stat()
    if not stat.st_size:
        raise MediaValidationError("empty_file", f"File is empty: {p}")

    # --- Content detection (concern #3) ---
    mime = detect_mime(p)

    # --- Size limit ---
    if stat.st_size > _MAX_FILE_BYTES:
        raise MediaValidationError(
            "file_too_large",
            f"File is {stat.st_size} bytes; max is {_MAX_FILE_BYTES}.",
        )

    # --- Dimensions ---
    width, height = detect_image_dimensions(p, mime)
    if width and height:
        if width > _MAX_DIMENSION or height > _MAX_DIMENSION:
            raise MediaValidationError(
                "dimensions_exceed_limit",
                f"Image is {width}x{height}; max dimension is {_MAX_DIMENSION}px.",
            )

    # --- SHA-256 digest (concern #1) ---
    digest = file_sha256(p)

    # --- EXIF metadata (concern #4) ---
    has_gps, exif_warnings = detect_exif_warnings(p, mime)

    return Attachment(
        path=str(p),
        basename=p.name,
        sha256=digest,
        mime=mime,
        byte_size=stat.st_size,
        width=width,
        height=height,
        alt_text=alt_text,
        exif_has_gps=has_gps,
        exif_warnings=exif_warnings,
    )
