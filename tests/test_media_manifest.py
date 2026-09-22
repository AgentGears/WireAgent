"""Tests for v0.2 M4a media manifest + multi-image safety logic.

ChatGPT's blocking concern: 'M4a added no net automated coverage.'
These tests cover the manifest preflight and the multi-image safety gates.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from webwire.safety.attachment import MediaValidationError
from webwire.safety.media_manifest import (
    MAX_IMAGES_PER_POST,
    preflight_manifest,
)


def _create_png(path: Path, w: int = 100, h: int = 100, color: tuple = (255, 0, 0)) -> None:
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes(color) * w
    idat = zlib.compress(raw)
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _create_text(path: Path) -> None:
    path.write_text("not an image")


# -- preflight: basic validation -------------------------------------------

def test_preflight_two_images(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    p2 = tmp_path / "b.png"
    _create_png(p2, 200, 200, (0, 255, 0))
    manifest = preflight_manifest([p1, p2])
    assert manifest.count == 2
    assert manifest.items[0].index == 0
    assert manifest.items[1].index == 1
    assert manifest.items[0].sha256 != manifest.items[1].sha256


def test_preflight_empty_list() -> None:
    with pytest.raises(MediaValidationError, match="empty_manifest"):
        preflight_manifest([])


def test_preflight_too_many_images(tmp_path: Path) -> None:
    paths = []
    for i in range(MAX_IMAGES_PER_POST + 1):
        p = tmp_path / f"img{i}.png"
        _create_png(p, 50 + i, 50 + i, (i * 50 % 256, 0, 0))
        paths.append(p)
    with pytest.raises(MediaValidationError, match="too_many_images"):
        preflight_manifest(paths)


def test_preflight_rejects_non_image(tmp_path: Path) -> None:
    p1 = tmp_path / "good.png"
    _create_png(p1)
    p2 = tmp_path / "bad.png"
    _create_text(p2)
    with pytest.raises(MediaValidationError, match="unsupported_format"):
        preflight_manifest([p1, p2])


def test_preflight_rejects_nonexistent(tmp_path: Path) -> None:
    p1 = tmp_path / "good.png"
    _create_png(p1)
    with pytest.raises(MediaValidationError, match="file_not_found"):
        preflight_manifest([p1, tmp_path / "missing.png"])


# -- preflight: duplicate detection ----------------------------------------

def test_preflight_rejects_duplicate_images(tmp_path: Path) -> None:
    """ChatGPT: duplicate-item policy. Same SHA-256 → rejected."""
    p1 = tmp_path / "original.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    p2 = tmp_path / "copy.png"
    p2.write_bytes(p1.read_bytes())  # exact copy
    with pytest.raises(MediaValidationError, match="duplicate_image"):
        preflight_manifest([p1, p2])


def test_preflight_allows_different_images_same_size(tmp_path: Path) -> None:
    p1 = tmp_path / "red.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    p2 = tmp_path / "blue.png"
    _create_png(p2, 100, 100, (0, 0, 255))
    manifest = preflight_manifest([p1, p2])
    assert manifest.count == 2


# -- preflight: one invalid rejects entire invocation ---------------------

def test_preflight_rejects_entire_manifest_on_one_invalid(tmp_path: Path) -> None:
    """ChatGPT: 'One invalid item rejects the entire invocation.
    Do not begin uploading a valid prefix.'"""
    p1 = tmp_path / "good1.png"
    _create_png(p1)
    p2 = tmp_path / "good2.png"
    _create_png(p2, 200, 200, (0, 255, 0))
    p3 = tmp_path / "bad.txt"
    _create_text(p3)
    with pytest.raises(MediaValidationError):
        preflight_manifest([p1, p2, p3])


# -- manifest immutability + identity ---------------------------------------

def test_manifest_is_frozen(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    manifest = preflight_manifest([p1])
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        manifest.items = ()  # type: ignore[misc]


def test_manifest_combined_hash_stable(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    p2 = tmp_path / "b.png"
    _create_png(p2, 150, 150, (0, 255, 0))
    m1 = preflight_manifest([p1, p2])
    m2 = preflight_manifest([p1, p2])
    assert m1.combined_hash == m2.combined_hash


def test_manifest_combined_hash_changes_on_order_swap(tmp_path: Path) -> None:
    """Order matters — [a, b] and [b, a] produce different combined hashes."""
    p1 = tmp_path / "a.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    p2 = tmp_path / "b.png"
    _create_png(p2, 100, 100, (0, 255, 0))
    m1 = preflight_manifest([p1, p2])
    m2 = preflight_manifest([p2, p1])
    assert m1.combined_hash != m2.combined_hash


def test_manifest_sha256_list_ordered(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    p2 = tmp_path / "b.png"
    _create_png(p2, 200, 200, (0, 255, 0))
    manifest = preflight_manifest([p1, p2])
    assert len(manifest.sha256_list) == 2
    assert manifest.sha256_list[0] == manifest.items[0].sha256
    assert manifest.sha256_list[1] == manifest.items[1].sha256


# -- alt text passthrough ---------------------------------------------------

def test_preflight_alt_text_passthrough(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    p2 = tmp_path / "b.png"
    _create_png(p2, 150, 150)
    manifest = preflight_manifest([p1, p2], alt_texts=["First image", "Second image"])
    assert manifest.items[0].attachment.alt_text == "First image"
    assert manifest.items[1].attachment.alt_text == "Second image"


def test_preflight_alt_text_padded_none(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    p2 = tmp_path / "b.png"
    _create_png(p2, 150, 150)
    manifest = preflight_manifest([p1, p2], alt_texts=["Only first"])
    assert manifest.items[0].attachment.alt_text == "Only first"
    assert manifest.items[1].attachment.alt_text is None  # padded


# -- MAX_IMAGES_PER_POST constant ------------------------------------------

def test_max_images_constant() -> None:
    assert MAX_IMAGES_PER_POST == 4


# -- manifest preview list --------------------------------------------------

def test_manifest_preview_list(tmp_path: Path) -> None:
    p1 = tmp_path / "a.png"
    _create_png(p1)
    manifest = preflight_manifest([p1])
    previews = manifest.to_preview_list()
    assert len(previews) == 1
    assert previews[0]["index"] == 0
    assert "basename" in previews[0]
    assert "sha256_prefix" in previews[0]
