"""Shared fixtures for write-capability tests.

`build_write_sample_inputs` is the single table of minimal valid inputs per
WRITE capability. The config-consistency tests rely on it: a new WRITE
capability that is not listed FAILS the suite, forcing its author to register
its action_type consciously (bucket + risk registry) and add a sample here.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

import pytest


def create_png(path: Path, w: int = 100, h: int = 100, color: tuple = (255, 0, 0)) -> None:
    """Minimal valid PNG (same byte format as test_media.py)."""
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


def build_write_sample_inputs(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Minimal valid input per WRITE capability."""
    red = tmp_path / "red.png"
    blue = tmp_path / "blue.png"
    create_png(red, color=(255, 0, 0))
    create_png(blue, color=(0, 0, 255))
    target = {"post_url": "https://x.com/a/status/1"}
    return {
        "bookmark_post": {**target},
        "like_post": {**target},
        "compose_post": {"text": "hello"},
        "post_text": {"text": "hello"},
        "reply_post": {**target, "text": "hello"},
        "quote_post": {**target, "text": "hello"},
        "post_photo": {"text": "hello", "image_path": str(red)},
        "post_multi_image": {"text": "hello", "image_paths": [str(red), str(blue)]},
        "reply_photo": {**target, "text": "hello", "image_path": str(red)},
        "quote_photo": {**target, "text": "hello", "image_path": str(red)},
        "reply_multi_image": {**target, "text": "hello", "image_paths": [str(red), str(blue)]},
        "quote_multi_image": {**target, "text": "hello", "image_paths": [str(red), str(blue)]},
        "delete_post": {**target},
    }


@pytest.fixture
def write_sample_inputs(tmp_path: Path) -> dict[str, dict[str, Any]]:
    return build_write_sample_inputs(tmp_path)
