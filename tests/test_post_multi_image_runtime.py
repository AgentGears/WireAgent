"""Runtime tests for post_multi_image.execute() — the 7 M4a failure paths.

ChatGPT's blocking concern (conversation 6a52de4c) required runtime coverage
for these cases. The 16 tests in test_media_manifest.py cover preflight only.
These tests exercise execute() directly via a _FakeMultiImageBroker that
injects failures at each safety gate.

ChatGPT's required sequence constraint:
"Tests must exercise post_multi_image.execute() directly, or a shared
primitive that execute() demonstrably delegates to. Tests that only exercise
M4b's analogous path would not retroactively cover M4a."

We test execute() directly. No primitive extraction yet — that happens in
Commit 2 when M4b's target-context hook makes the right boundary visible.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any, Optional

from webwire.capabilities.post_multi_image import PostMultiImageCapability
from webwire.envelope import ok_result, soft_failure
from webwire.safety import WriteIntent

# ---------------------------------------------------------------------------
# PNG fixture helper (same as test_media_manifest.py — minimal valid PNG)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# _FakeMultiImageBroker — configurable failure injection
# ---------------------------------------------------------------------------
#
# The broker records every call so tests can assert "submit never happened"
# and "close_composer was called" (the abort-and-cleanup contract).
#
# Failure knobs:
#   fail_attach_on_item:   1-based index; attach_media returns failure on that item
#   attach_count_sequence: list of counts count_attachments returns after each upload.
#                          Default: [1, 2, ..., N] (happy path). Override to inject
#                          count mismatch (e.g. [1, 3] for "got 3 after 2nd upload").
#   fail_ready_on_item:    verify_attachment_ready returns failure on that item
#   composer_text_final:   text read_composer_text returns after all uploads.
#                          Default: echo the filled text. Override to inject mutation.
#   kill_before_submit:    if True, _kill.tripped() returns True at final check
#   submit_result_count:   media count the post-submit verification sees (default: expected)


class _FakeKill:
    def __init__(self, tripped: bool = False) -> None:
        self._tripped = tripped

    def tripped(self) -> bool:
        return self._tripped

    def trip(self) -> None:
        self._tripped = True


class _FakeMultiImageBroker:
    """Fake write broker for post_multi_image.execute().

    Defaults to the happy path. Override attributes to inject failures."""

    def __init__(
        self,
        expected_text: str = "test post",
        expected_count: int = 2,
    ) -> None:
        self._expected_text = expected_text
        self._expected_count = expected_count

        # Failure injection knobs (defaults = happy path).
        self.fail_attach_on_item: Optional[int] = None
        self.attach_count_sequence: Optional[list[int]] = None
        self.fail_ready_on_item: Optional[int] = None
        self.composer_text_final: Optional[str] = None
        self._kill: Optional[_FakeKill] = None
        # Post-submit media count (for the transcoding/order tests).
        self.post_submit_media_count: Optional[int] = None
        self.post_submit_text_ok: bool = True

        # Call recording.
        self.fill_calls: list[str] = []
        self.attach_calls: list[str] = []
        self.ready_calls: int = 0
        self.count_calls: int = 0
        self.read_text_calls: int = 0
        self.submit_clicked: bool = False
        self.close_composer_calls: int = 0
        self._uploads_done = 0  # tracks how many attach_media calls succeeded

    # -- composer methods ----------------------------------------------------

    async def fill_composer(self, text: str) -> Any:
        self.fill_calls.append(text)
        return ok_result(data={"filled": True})

    async def read_composer_text(self) -> Any:
        self.read_text_calls += 1
        # The capability reads text after all uploads; return the configured value.
        text = self.composer_text_final if self.composer_text_final is not None else self._expected_text
        return ok_result(data={"composer_text": text})

    async def click_submit(self) -> Any:
        self.submit_clicked = True
        return ok_result(data={"clicked": True})

    async def close_composer(self) -> Any:
        self.close_composer_calls += 1
        return ok_result(data={"closed": True})

    # -- media methods -------------------------------------------------------

    async def attach_media(self, image_path: str) -> Any:
        self.attach_calls.append(image_path)
        item_num = len(self.attach_calls)  # 1-based
        if self.fail_attach_on_item is not None and item_num == self.fail_attach_on_item:
            # Return ok=False so execute()'s `if not attach_r.ok` branch fires.
            return soft_failure(f"injected attach failure on item {item_num}")
        # Simulate success.
        self._uploads_done += 1
        return ok_result(data={"attached": True})

    async def verify_attachment_ready(self) -> Any:
        self.ready_calls += 1
        item_num = self.ready_calls  # 1-based
        if self.fail_ready_on_item is not None and item_num == self.fail_ready_on_item:
            return soft_failure(f"preview never stabilized on item {item_num}")
        return ok_result(data={"ready": True})

    async def count_attachments(self) -> Any:
        self.count_calls += 1
        call_idx = self.count_calls  # 1-based
        if self.attach_count_sequence is not None:
            # Use the sequence if provided; pad with expected if exhausted.
            idx = call_idx - 1
            if idx < len(self.attach_count_sequence):
                return ok_result(data={"count": self.attach_count_sequence[idx]})
        # Default: cumulative count matches uploads done so far.
        return ok_result(data={"count": self._uploads_done})

    # -- capture_pre_submit_ids / capture_new_post_id stubs ------------------
    # These are module-level functions in post_submit.py, not broker methods.
    # The capability calls them directly, but they reference broker._sb for
    # CDP access. Since our fake has no _sb, we monkeypatch the module functions
    # in the tests that reach the submit path.


# ---------------------------------------------------------------------------
# Intent builder — bypasses preflight by using real temp PNGs
# ---------------------------------------------------------------------------

def _make_two_pngs(tmp_path: Path) -> list[Path]:
    p1 = tmp_path / "red.png"
    p2 = tmp_path / "blue.png"
    _create_png(p1, 100, 100, (255, 0, 0))
    _create_png(p2, 150, 150, (0, 0, 255))
    return [p1, p2]


def _make_intent(tmp_path: Path, image_paths: list[Path], text: str = "test post") -> WriteIntent:
    """Build a WriteIntent the same way PostMultiImageCapability.compose() does."""
    cap = PostMultiImageCapability()
    return cap.compose({"text": text, "image_paths": [str(p) for p in image_paths]}, actor_identity="infaag")


# ---------------------------------------------------------------------------
# Test 1: Item 2 attachment failure
# ---------------------------------------------------------------------------

async def test_item2_attachment_failure_aborts_no_submit(tmp_path: Path) -> None:
    """ChatGPT case 1: item 1 attaches, item 2 fails.
    - close_composer called
    - click_submit never called
    - no post-submit verifier called"""
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)
    broker.fail_attach_on_item = 2  # second attach fails

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    # Abort happened.
    assert r.ok is False
    assert "attachment_upload_failed" in (r.data or {}).get("result", "")
    # close_composer WAS called (cleanup).
    assert broker.close_composer_calls == 1
    # click_submit NEVER called.
    assert broker.submit_clicked is False
    # Only 2 attach attempts (item 1 success + item 2 fail).
    assert len(broker.attach_calls) == 2


# ---------------------------------------------------------------------------
# Test 2: Runtime count mismatch
# ---------------------------------------------------------------------------

async def test_count_mismatch_aborts_no_submit(tmp_path: Path) -> None:
    """ChatGPT case 2: expected 2, broker reports 3 after 2nd upload.
    Abort and cleanup; no submit."""
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)
    # After upload 1: count=1 (ok). After upload 2: count=3 (mismatch!).
    broker.attach_count_sequence = [1, 3]

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is False
    assert "attachment_count_mismatch" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


# ---------------------------------------------------------------------------
# Test 3: Preview readiness failure
# ---------------------------------------------------------------------------

async def test_preview_not_ready_aborts_no_submit(tmp_path: Path) -> None:
    """ChatGPT case 3: attachment accepted but preview never stabilizes.
    Abort and cleanup; no submit."""
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)
    broker.fail_ready_on_item = 1  # first preview never ready

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is False
    assert "attachment_not_ready" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


# ---------------------------------------------------------------------------
# Test 4: Composer mutation after batch
# ---------------------------------------------------------------------------

async def test_composer_text_mutation_after_batch_aborts(tmp_path: Path) -> None:
    """ChatGPT case 4: final composer text differs from approved text.
    Abort and cleanup; no submit."""
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths, text="approved text")
    broker = _FakeMultiImageBroker(expected_text="approved text", expected_count=2)
    # After uploads complete, composer text has been mutated.
    broker.composer_text_final = "MUTATED BY PICKER"

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is False
    assert "pre_submit_mismatch" in (r.data or {}).get("result", "")
    assert broker.close_composer_calls == 1
    assert broker.submit_clicked is False


# ---------------------------------------------------------------------------
# Test 5: Kill after authorization, before click
# ---------------------------------------------------------------------------

async def test_kill_before_submit_aborts_after_authorization(tmp_path: Path) -> None:
    """ChatGPT case 5: kill switch trips at the final boundary.
    - composer closes
    - terminal state distinguishes authorized-but-not-submitted
    - no submit

    NOTE: The current M4a code checks kill via `broker._kill.tripped()`.
    ChatGPT's terminal-state taxonomy (authorized_but_not_submitted_and_cleaned)
    is NOT yet implemented in the code — this test documents the CURRENT
    behavior (aborts with killed_before_submit) and will need updating when
    the terminal-state taxonomy lands. That's a Commit 1 stretch goal, not
    a blocker for closing the test-coverage gap.
    """
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)
    broker._kill = _FakeKill(tripped=True)

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is False
    assert "killed_before_submit" in (r.data or {}).get("result", "")
    # Cleanup happened (close_composer).
    assert broker.close_composer_calls == 1
    # No submit.
    assert broker.submit_clicked is False


# ---------------------------------------------------------------------------
# Test 6: Transcoding-honest verification
# ---------------------------------------------------------------------------

async def test_transcoding_honest_no_false_byte_identity(tmp_path: Path, monkeypatch) -> None:
    """ChatGPT case 6: source hashes cannot be compared after X processing.
    Count and per-preview evidence succeed, but result must NOT claim
    source_byte_equivalence_verified=True.

    This test reaches the submit path, so we monkeypatch the post-submit
    capture functions to avoid needing a real browser/CDP.
    """
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)

    # Monkeypatch the post-submit capture functions AND the text/media helpers
    # used by execute(). These helpers reference broker._sb (CDP), which the
    # fake broker doesn't have.
    import webwire.capabilities.post_multi_image as pmi_mod

    async def fake_capture_pre_submit_ids(b):
        return set()

    async def fake_capture_new_post_id(b, pre_ids, exclude_ids=None):
        return ("999", "https://x.com/infaag/status/999")

    async def fake_verify_text(broker, posted_url, normalized):
        return True

    async def fake_count_post_media(broker, posted_url):
        return 2  # matches expected_count

    monkeypatch.setattr(pmi_mod, "capture_pre_submit_ids", fake_capture_pre_submit_ids)
    monkeypatch.setattr(pmi_mod, "capture_new_post_id", fake_capture_new_post_id)
    monkeypatch.setattr(pmi_mod, "_verify_text", fake_verify_text)
    monkeypatch.setattr(pmi_mod, "_count_post_media", fake_count_post_media)

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is True
    data = r.data
    # The critical assertion: byte equivalence is NOT claimed.
    assert data["source_byte_equivalence_verified"] is False
    assert data["media_count_verified"] is True
    # Per-item evidence also honest.
    for item in data["media_items"]:
        assert item["source_byte_equivalence_verified"] is False


# ---------------------------------------------------------------------------
# Test 7: Rendered-order ambiguity
# ---------------------------------------------------------------------------

async def test_rendered_order_ambiguity_honest_false(tmp_path: Path, monkeypatch) -> None:
    """ChatGPT case 7: DOM enumeration differs from source order, but all items
    can still be attachment-verified. Result must report order_preserved=False
    and must not make an accidental index-based identity claim.

    Current M4a code already hardcodes media_order_verified=False (honest),
    because X's DOM doesn't expose per-image source identity. This test
    locks that honesty in: even on the happy path, order is NOT claimed.
    """
    paths = _make_two_pngs(tmp_path)
    intent = _make_intent(tmp_path, paths)
    broker = _FakeMultiImageBroker(expected_text="test post", expected_count=2)

    import webwire.capabilities.post_multi_image as pmi_mod

    async def fake_capture_pre_submit_ids(b):
        return set()

    async def fake_capture_new_post_id(b, pre_ids, exclude_ids=None):
        return ("999", "https://x.com/infaag/status/999")

    async def fake_verify_text(broker, posted_url, normalized):
        return True

    async def fake_count_post_media(broker, posted_url):
        return 2  # matches expected_count

    monkeypatch.setattr(pmi_mod, "capture_pre_submit_ids", fake_capture_pre_submit_ids)
    monkeypatch.setattr(pmi_mod, "capture_new_post_id", fake_capture_new_post_id)
    monkeypatch.setattr(pmi_mod, "_verify_text", fake_verify_text)
    monkeypatch.setattr(pmi_mod, "_count_post_media", fake_count_post_media)

    cap = PostMultiImageCapability()
    r = await cap.execute(intent, broker)

    assert r.ok is True
    data = r.data
    # Order is NOT claimed — X DOM can't map rendered position back to source.
    assert data["media_order_verified"] is False
    # But count and attachment verification still succeed.
    assert data["media_count_verified"] is True
    assert data["all_media_attachments_verified"] is True
