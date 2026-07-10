"""Tests for v0.2 M1 media validation module."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from webwire.safety.attachment import (
    Attachment,
    MediaValidationError,
    detect_mime,
    file_sha256,
    validate_media_file,
)


def _create_png(path: Path, w: int = 100, h: int = 100) -> None:
    """Create a minimal valid PNG."""
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes((255, 0, 0)) * w
    idat = zlib.compress(raw)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _create_jpeg(path: Path) -> None:
    """Create a minimal valid JPEG (just the SOI marker + EOI)."""
    with open(path, "wb") as f:
        f.write(b"\xff\xd8\xff\xe0" + b"\x00" * 100 + b"\xff\xd9")


def _create_text_file(path: Path) -> None:
    """Create a non-image file."""
    path.write_text("this is not an image")


# -- detect_mime -----------------------------------------------------------

def test_detect_mime_png(tmp_path: Path) -> None:
    p = tmp_path / "test.png"
    _create_png(p)
    assert detect_mime(p) == "image/png"


def test_detect_mime_jpeg(tmp_path: Path) -> None:
    p = tmp_path / "test.jpg"
    _create_jpeg(p)
    assert detect_mime(p) == "image/jpeg"


def test_detect_mime_rejects_non_image(tmp_path: Path) -> None:
    p = tmp_path / "fake.png"
    _create_text_file(p)
    with pytest.raises(MediaValidationError, match="unsupported_format"):
        detect_mime(p)


# -- file_sha256 -----------------------------------------------------------

def test_file_sha256_stable(tmp_path: Path) -> None:
    p = tmp_path / "test.png"
    _create_png(p)
    assert file_sha256(p) == file_sha256(p)


def test_file_sha256_changes_on_content_change(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    p2 = tmp_path / "b.png"
    _create_png(p1, 100, 100)
    _create_png(p2, 200, 200)
    assert file_sha256(p1) != file_sha256(p2)


# -- validate_media_file ---------------------------------------------------

def test_validate_media_file_success(tmp_path: Path) -> None:
    p = tmp_path / "test.png"
    _create_png(p)
    att = validate_media_file(p)
    assert att.basename == "test.png"
    assert att.mime == "image/png"
    assert att.byte_size > 0
    assert att.sha256  # non-empty
    assert att.exif_has_gps is False  # PNG has no EXIF


def test_validate_media_file_rejects_nonexistent(tmp_path: Path) -> None:
    with pytest.raises(MediaValidationError, match="file_not_found"):
        validate_media_file(tmp_path / "nonexistent.png")


def test_validate_media_file_rejects_non_image(tmp_path: Path) -> None:
    p = tmp_path / "fake.png"
    _create_text_file(p)
    with pytest.raises(MediaValidationError, match="unsupported_format"):
        validate_media_file(p)


def test_validate_media_file_rejects_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "empty.png"
    p.write_bytes(b"")
    with pytest.raises(MediaValidationError, match="empty_file"):
        validate_media_file(p)


def test_validate_media_file_with_upload_roots_allowed(tmp_path: Path) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    p = media_dir / "test.png"
    _create_png(p)
    att = validate_media_file(p, upload_roots=[media_dir])
    assert att.basename == "test.png"


def test_validate_media_file_with_upload_roots_rejected(tmp_path: Path) -> None:
    media_dir = tmp_path / "allowed"
    media_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    p = outside_dir / "test.png"
    _create_png(p)
    with pytest.raises(MediaValidationError, match="path_outside_upload_roots"):
        validate_media_file(p, upload_roots=[media_dir])


def test_validate_media_file_rejects_directory(tmp_path: Path) -> None:
    d = tmp_path / "somedir"
    d.mkdir()
    with pytest.raises(MediaValidationError, match="not_regular_file"):
        validate_media_file(d)


# -- Attachment model -------------------------------------------------------

def test_attachment_immutable() -> None:
    att = Attachment(
        path="/tmp/test.png", basename="test.png", sha256="abc123",
        mime="image/png", byte_size=100,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        att.basename = "changed.png"  # type: ignore[misc]


def test_attachment_digest_prefix() -> None:
    att = Attachment(
        path="/x", basename="t.png", sha256="abcdef1234567890",
        mime="image/png", byte_size=1,
    )
    assert att.digest_prefix() == "abcdef123456"


def test_attachment_preview_dict() -> None:
    att = Attachment(
        path="/x", basename="photo.png", sha256="abcdef1234567890",
        mime="image/png", byte_size=1024, width=800, height=600,
        alt_text="A test image", exif_has_gps=False,
    )
    d = att.to_preview_dict()
    assert d["basename"] == "photo.png"
    assert d["dimensions"] == "800x600"
    assert d["sha256_prefix"] == "abcdef123456"
    assert d["alt_text"] == "A test image"
