"""Runtime tests for quote_multi_image (M4c, 2026-09-22).

Shared-primitive constraint (on record): tests exercise the capability's
execute() — which demonstrably delegates to safety.media_compose.run_media_compose —
covering the shared harness through M4c's composition, complementing the M4a
(post) and M4b (reply) suites.

Cases:
- Quote-specific: quote-open failure aborts BEFORE any media (code
  quote_action_not_available); quote context opens before fill before media.
- M4a gates through the quote path: item-2 attach failure, count mismatch,
  preview-not-ready, composer mutation, kill-before-submit (abort-and-
  cleanup, submit never clicked).
- Honest reporting: dual attachment (quote by execution path, media by DOM
  count) reported SEPARATELY; order and byte equivalence never claimed.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Optional

import webwire.capabilities.quote_multi_image as qmi_mod
from webwire.capabilities.quote_multi_image import QuoteMultiImageCapability
from webwire.envelope import ok_result, soft_failure
from webwire.safety import WriteIntent

TARGET_URL = "https://x.com/infaag/status/2102451358305771541"
TARGET_ID = "2102451358305771541"


def _create_png(path: Path, w: int = 100, h: int = 100, color: tuple = (255, 0, 0)) -> None:
    def chunk(ctype, data):
        c = ctype + data
        return struct.pack(">I", len(data)) + c + struct.pack(">I", zlib.crc32(c) & 0xFFFFFFFF)
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)
    raw = b""
    for _ in range(h):
        raw += b"\x00" + bytes(color) * w
    with open(path, "wb") as f:
        f.write(sig + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def _make_two_pngs(tmp_path: Path) -> list[Path]:
    p1 = tmp_path / "red.png"
    p2 = tmp_path / "blue.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    _create_png(p2, 150, 150, (0, 0, 255))
    return [p1, p2]


def _make_intent(tmp_path: Path, paths: list[Path], text: str = "test quote") -> WriteIntent:
    cap = QuoteMultiImageCapability()
    return cap.compose(
        {"post_url": TARGET_URL, "text": text, "image_paths": [str(p) for p in paths]},
        actor_identity="infaag",
    )


class _FakeKill:
    def __init__(self, tripped: bool = False) -> None:
        self._tripped = tripped

    def tripped(self) -> bool:
        return self._tripped


class _FakeQuoteMultiImageBroker:
    """Fake write broker recording the quote-target flow. Quote hook
    contract: open_quote_on_target FIRST, then fill_quote_composer, then
    media. Failure knobs mirror the M4a/M4b fakes."""

    def __init__(self, expected_text: str = "test quote", expected_count: int = 2) -> None:
        self._expected_text = expected_text
        self._expected_count = expected_count
        self._uploads_done = 0

        self.fail_open_quote: bool = False
        self.fail_attach_on_item: Optional[int] = None
        self.fail_ready_on_item: Optional[int] = None
        self.attach_count_sequence: Optional[list[int]] = None
        self.composer_text_final: Optional[str] = None
        self._kill: Optional[_FakeKill] = None

        self.open_quote_calls: list[tuple[str, str]] = []
        self.fill_calls: list[str] = []
        self.attach_calls: list[str] = []
        self.count_calls = 0
        self.read_text_calls = 0
        self.submit_clicked = False
        self.close_composer_calls = 0

    async def open_quote_on_target(self, post_url: str, target_post_id: str) -> Any:
        self.open_quote_calls.append((post_url, target_post_id))
        if self.fail_open_quote:
            return soft_failure("repost→Quote affordance not available on target article")
        return ok_result(data={"opened": True})

    async def fill_quote_composer(self, text: str) -> Any:
        self.fill_calls.append(text)
        return ok_result(data={"filled": True})

    async def attach_media(self, image_path: str) -> Any:
        self.attach_calls.append(image_path)
        n = len(self.attach_calls)
        if self.fail_attach_on_item is not None and n == self.fail_attach_on_item:
            return soft_failure(f"injected attach failure on item {n}")
        self._uploads_done += 1
        return ok_result(data={"attached": True})

    async def verify_attachment_ready(self) -> Any:
        if self.fail_ready_on_item is not None and len(self.attach_calls) == self.fail_ready_on_item:
            return soft_failure("preview never stabilized")
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        self.count_calls += 1
        if self.attach_count_sequence is not None and self.count_calls <= len(self.attach_count_sequence):
            return ok_result(data={"count": self.attach_count_sequence[self.count_calls - 1]})
        return ok_result(data={"count": self._uploads_done})

    async def read_composer_text(self) -> Any:
        self.read_text_calls += 1
        text = self.composer_text_final if self.composer_text_final is not None else self._expected_text
        return ok_result(data={"composer_text": text})

    async def click_submit(self) -> Any:
        self.submit_clicked = True
        return ok_result(data={"clicked": True})

    async def close_composer(self) -> Any:
        self.close_composer_calls += 1
        return ok_result(data={"closed": True})


def _patch_post_submit(monkeypatch, *, media_count=2) -> None:
    async def fake_pre_submit_ids(b):
        return set()

    async def fake_new_post_id(b, pre_ids, exclude_ids=None):
        return ("777", "https://x.com/infaag/status/777")

    async def fake_verify_text(broker, posted_url, normalized):
        return True

    async def fake_count_media(broker, posted_url):
        return media_count

    monkeypatch.setattr(qmi_mod, "capture_pre_submit_ids", fake_pre_submit_ids)
    monkeypatch.setattr(qmi_mod, "capture_new_post_id", fake_new_post_id)
    monkeypatch.setattr(qmi_mod, "_verify_text", fake_verify_text)
    monkeypatch.setattr(qmi_mod, "_count_post_media", fake_count_media)


# ---------------------------------------------------------------------------
# Quote-specific
# ---------------------------------------------------------------------------

async def test_quote_open_failure_aborts_before_any_media(tmp_path: Path) -> None:
    """Hook contract: if the quote context cannot open on the target,
    NOTHING else happens — no fill, no media, no cleanup needed, no submit."""
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    broker.fail_open_quote = True

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "quote_action_not_available" in (r.data or {}).get("result", "")
    assert broker.attach_calls == [], "no media may attach before the quote opens"
    assert broker.fill_calls == []
    assert broker.submit_clicked is False


async def test_quote_opens_before_fill_and_media(tmp_path: Path, monkeypatch) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    _patch_post_submit(monkeypatch)

    r = await QuoteMultiImageCapability().execute(intent, broker)
    assert r.ok is True

    assert len(broker.open_quote_calls) == 1
    assert broker.open_quote_calls[0][1] == TARGET_ID
    assert len(broker.fill_calls) == 1
    assert len(broker.attach_calls) == 2


# ---------------------------------------------------------------------------
# M4a gates through the quote path
# ---------------------------------------------------------------------------

async def test_item2_attach_failure_aborts_no_submit(tmp_path: Path) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    broker.fail_attach_on_item = 2

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "attachment_upload_failed" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False
    assert len(broker.attach_calls) == 2


async def test_count_mismatch_aborts_no_submit(tmp_path: Path) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    broker.attach_count_sequence = [1, 3]

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "attachment_count_mismatch" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


async def test_preview_not_ready_aborts_no_submit(tmp_path: Path) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    broker.fail_ready_on_item = 1

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "attachment_not_ready" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


async def test_composer_text_mutation_after_media_aborts(tmp_path: Path) -> None:
    """M3b caution via the multi-image path: the media picker displaces the
    quote composition — composition atomicity aborts before submit."""
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path), text="approved quote")
    broker = _FakeQuoteMultiImageBroker(expected_text="approved quote")
    broker.composer_text_final = "MUTATED BY PICKER"

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "pre_submit_mismatch" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


async def test_kill_before_submit_aborts_after_authorization(tmp_path: Path) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    broker._kill = _FakeKill(tripped=True)

    r = await QuoteMultiImageCapability().execute(intent, broker)

    assert r.ok is False
    assert "killed_before_submit" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


# ---------------------------------------------------------------------------
# Honest reporting
# ---------------------------------------------------------------------------

async def test_happy_path_dual_attachment_honest_reporting(tmp_path: Path, monkeypatch) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    _patch_post_submit(monkeypatch, media_count=2)

    r = await QuoteMultiImageCapability().execute(intent, broker)
    assert r.ok is True
    data = r.data
    assert data["result"] == "quote_multi_image_posted_and_target_verified"
    # Dual attachment, SEPARATELY reported:
    assert data["quote_attachment_verified"] is True
    assert data["quote_attachment_verified_by"] == "execution_path"
    assert data["quote_attachment_dom_verified"] is False
    assert data["media_count_verified"] is True
    assert data["all_media_attachments_verified"] is True
    # Honesty locks:
    assert data["media_order_verified"] is False
    assert data["source_byte_equivalence_verified"] is False
    for item in data["media_items"]:
        assert item["source_byte_equivalence_verified"] is False


async def test_media_count_mismatch_reported(tmp_path: Path, monkeypatch) -> None:
    intent = _make_intent(tmp_path, _make_two_pngs(tmp_path))
    broker = _FakeQuoteMultiImageBroker()
    _patch_post_submit(monkeypatch, media_count=1)

    r = await QuoteMultiImageCapability().execute(intent, broker)
    assert r.ok is True
    data = r.data
    assert data["result"] == "quote_multi_image_posted_text_verified_media_count_mismatch"
    assert data["media_count_verified"] is False
    # Quote attachment still reported separately and honestly.
    assert data["quote_attachment_verified_by"] == "execution_path"
