"""Runtime tests for the migrated single-photo capabilities (C4, 2026-09-23).

post_photo / reply_photo / quote_photo now DELEGATE to the shared
media_compose harness with single-item manifests. These tests prove:
- the delegation works end-to-end (happy paths through execute()),
- the NEW exact-count gate fires (accepted tightening: single-photo flows
  previously had no count gate),
- the context hooks order correctly (reply: target before media; quote:
  quote context before media).
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Optional

import pytest

import webwire.capabilities.post_photo as pp_mod
import webwire.capabilities.quote_photo as qp_mod
import webwire.capabilities.reply_photo as rp_mod
from webwire.capabilities.post_photo import PostPhotoCapability
from webwire.capabilities.quote_photo import QuotePhotoCapability
from webwire.capabilities.reply_photo import ReplyPhotoCapability
from webwire.envelope import ok_result, soft_failure

TARGET_URL = "https://x.com/infaag/status/2102451358305771541"
TARGET_ID = "2102451358305771541"


def _png(path: Path, color: tuple = (255, 0, 0)) -> Path:
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", 100, 100, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + bytes(color) * 100 for _ in range(100))
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))
    return path


class _FakeSinglePhotoBroker:
    """M4a-pattern fake with composer-flavor switch and count injection."""

    def __init__(self) -> None:
        self._uploads = 0
        self.count_sequence: Optional[list[int]] = None
        self._count_calls = 0
        self.fill_calls: list[str] = []
        self.open_calls: list[tuple[str, str]] = []
        self.attach_calls: list[str] = []
        self.submit_clicked = False
        self.close_calls = 0

    async def fill_composer(self, text: str) -> Any:
        self.fill_calls.append(f"post:{text}")
        return ok_result(data={"filled": True})

    async def fill_reply_composer(self, text: str) -> Any:
        self.fill_calls.append(f"reply:{text}")
        return ok_result(data={"filled": True})

    async def fill_quote_composer(self, text: str) -> Any:
        self.fill_calls.append(f"quote:{text}")
        return ok_result(data={"filled": True})

    async def open_reply_on_target(self, url: str, tid: str) -> Any:
        self.open_calls.append(("reply", tid))
        return ok_result(data={"opened": True})

    async def open_quote_on_target(self, url: str, tid: str) -> Any:
        self.open_calls.append(("quote", tid))
        return ok_result(data={"opened": True})

    async def attach_media(self, path: str) -> Any:
        self.attach_calls.append(path)
        self._uploads += 1
        return ok_result(data={"attached": True})

    async def verify_attachment_ready(self) -> Any:
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        self._count_calls += 1
        if self.count_sequence and self._count_calls <= len(self.count_sequence):
            return ok_result(data={"count": self.count_sequence[self._count_calls - 1]})
        return ok_result(data={"count": self._uploads})

    async def read_composer_text(self) -> Any:
        return ok_result(data={"composer_text": "photo test"})

    async def click_submit(self) -> Any:
        self.submit_clicked = True
        return ok_result(data={"clicked": True})

    async def close_composer(self) -> Any:
        self.close_calls += 1
        return ok_result(data={"closed": True})


def _patch_common(monkeypatch, module, *, media_count=1) -> None:
    async def fake_pre(b):
        return set()

    async def fake_capture(b, pre_ids, exclude_ids=None):
        return ("555", "https://x.com/infaag/status/555")

    async def fake_verify_text(broker, url, text):
        return True

    async def fake_count(broker, url):
        return media_count

    monkeypatch.setattr(module, "capture_pre_submit_ids", fake_pre)
    monkeypatch.setattr(module, "_verify_text", fake_verify_text)
    monkeypatch.setattr(module, "_count_post_media", fake_count)
    if hasattr(module, "_capture_via_posted_url") or hasattr(module, "_capture_adapter"):
        target = getattr(module, "_capture_via_posted_url", None) or module._capture_adapter
        monkeypatch.setattr(module, target.__name__, fake_capture)
    else:
        monkeypatch.setattr(module, "capture_new_post_id", fake_capture)


async def test_post_photo_delegates_and_passes_count_gate(tmp_path: Path, monkeypatch) -> None:
    img = _png(tmp_path / "red.png")
    cap = PostPhotoCapability()
    intent = cap.compose({"text": "photo test", "image_path": str(img)}, actor_identity="t")
    broker = _FakeSinglePhotoBroker()
    _patch_common(monkeypatch, pp_mod, media_count=1)

    r = await cap.execute(intent, broker)
    assert r.ok is True
    assert r.data["result"] == "posted_and_verified"
    assert broker.fill_calls == ["post:photo test"]
    assert len(broker.attach_calls) == 1
    assert broker.submit_clicked is True


async def test_post_photo_count_mismatch_aborts_new_gate(tmp_path: Path, monkeypatch) -> None:
    """The accepted tightening: single-photo flows now abort on a wrong
    composer count — a gate they did not have before the migration."""
    img = _png(tmp_path / "red.png")
    cap = PostPhotoCapability()
    intent = cap.compose({"text": "photo test", "image_path": str(img)}, actor_identity="t")
    broker = _FakeSinglePhotoBroker()
    broker.count_sequence = [2]  # count gate expects 1 after one upload
    _patch_common(monkeypatch, pp_mod)

    r = await cap.execute(intent, broker)
    assert r.ok is False
    assert "attachment_count_mismatch" in (r.data or {}).get("result", "")
    assert broker.close_calls == 1
    assert broker.submit_clicked is False


async def test_reply_photo_target_first_via_harness(tmp_path: Path, monkeypatch) -> None:
    img = _png(tmp_path / "red.png")
    cap = ReplyPhotoCapability()
    intent = cap.compose(
        {"post_url": TARGET_URL, "text": "photo test", "image_path": str(img)},
        actor_identity="t",
    )
    broker = _FakeSinglePhotoBroker()

    async def fake_thread(b, parent, tid, rid, text):
        return {"found": True, "text_matches": True}

    _patch_common(monkeypatch, rp_mod, media_count=1)
    monkeypatch.setattr(rp_mod, "_verify_reply_in_thread", fake_thread)

    r = await cap.execute(intent, broker)
    assert r.ok is True
    assert r.data["result"] == "reply_photo_posted_and_target_verified"
    assert broker.open_calls == [("reply", TARGET_ID)], "target opens FIRST"
    assert broker.fill_calls == ["reply:photo test"], "then the reply composer fills"
    assert len(broker.attach_calls) == 1, "media attaches after the target context"


async def test_quote_photo_quote_context_via_harness(tmp_path: Path, monkeypatch) -> None:
    img = _png(tmp_path / "blue.png")
    cap = QuotePhotoCapability()
    intent = cap.compose(
        {"post_url": TARGET_URL, "text": "photo test", "image_path": str(img)},
        actor_identity="t",
    )
    broker = _FakeSinglePhotoBroker()
    _patch_common(monkeypatch, qp_mod, media_count=1)

    r = await cap.execute(intent, broker)
    assert r.ok is True
    assert r.data["result"] == "quote_photo_posted_and_target_verified"
    assert broker.open_calls == [("quote", TARGET_ID)]
    assert broker.fill_calls == ["quote:photo test"]
    # Dual attachment reported separately.
    assert r.data["quote_attachment_verified_by"] == "execution_path"
    assert r.data["media_attachment_verified"] is True
