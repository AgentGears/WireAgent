"""Shared media-compose harness — the M4a ordered media-manifest transaction,
parameterized by the target-context hook (M4b extraction, 2026-09-22).

This is the shared primitive the M4b decision required: M4a's
post_multi_image.execute() and M4b's reply_multi_image.execute() both
DEMONSTRABLY DELEGATE here, so tests exercising this harness cover both.

Gate sequence (M4a's, verbatim):
  1. Recompute every item digest before ANY upload (one changed → reject).
  2. Target-context hook (if any) opens the reply context FIRST — the M3a
     lesson: media never attaches before the target is established.
  3. Fill composer text.
  4. Ordered uploads: attach → verify preview ready → EXACT-COUNT gate after
     each item (i+1 after upload i+1). One invalid → abort, never partial.
  5. Composition re-verification (composer read-back must equal approved text).
  6. Final exact-count check.
  7. Pre-submit identity capture.
  8. Final kill check ("hand on the button").
  9. Submit → identity-aware post-submit capture → text + media verification.
Any failure after the composer opens → abort-and-cleanup (close composer).

Post-submit steps (capture + verify) are injected as callables
(``PostSubmitHooks``) resolved from the CALLING capability's module at call
time — existing monkeypatch-based tests keep working unchanged, and the
definitions live once in ``webwire.safety.media_verify``.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from webwire.envelope import ActionResult, ok_result
from webwire.safety.media_verify import normalize_for_compare

logger = logging.getLogger(__name__)

__all__ = ["MediaComposeSpec", "PostSubmitHooks", "run_media_compose"]


@dataclass(frozen=True)
class PostSubmitHooks:
    """Callables the harness uses for identity capture + post-submit verify.
    Injected per-capability so module-level monkeypatching in tests stays
    authoritative; the definitions live once in safety/media_verify.py."""
    capture_pre_submit_ids: Callable[[Any], Awaitable[set]]
    # Varargs-typed: implementations take keyword extras (exclude_ids=...) and
    # tests monkeypatch with narrower signatures; the contract is the RETURN.
    capture_new_post_id: Callable[..., Awaitable[tuple[Optional[str], Optional[str]]]]
    verify_text: Callable[..., Awaitable[bool]]
    count_media: Callable[..., Awaitable[int]]


@dataclass(frozen=True)
class MediaComposeSpec:
    """One media-compose transaction. `items` mirrors the M4a manifest items
    (index/source_path/sha256 dicts); target fields drive the context hook."""
    normalized_text: str
    items: list[dict[str, Any]]
    expected_count: int
    # Target-context hook (target_post_id None → plain post composer):
    target_post_url: Optional[str] = None
    target_post_id: Optional[str] = None
    # Which context the hook opens: "reply" (open_reply_on_target) or "quote"
    # (open_quote_on_target). Same contract either way: context BEFORE media.
    context: str = "reply"
    # Failure code when the reply-context open fails (capability vocabulary);
    # the quote context uses quote_action_not_available (quote_photo's code).
    target_open_failure_code: str = "target_not_found_before_reply"
    # IDs the post-submit capture must exclude (e.g. the quoted/replied target).
    exclude_ids: frozenset[str] = frozenset()


@dataclass
class ComposeOutcome:
    """What the harness established. Capabilities format their own final
    ActionResult (result-code vocabulary + honest reporting) from this."""
    posted: bool                     # submit clicked and new post id captured
    code: str                        # result code on failure/degraded paths
    message: str
    posted_url: Optional[str] = None
    posted_post_id: Optional[str] = None
    text_verified: bool = False
    media_count: int = 0


def failure(code: str, message: str) -> ActionResult:
    """Clean pre-submit failure — no public side effect."""
    from super_browser.results import ActionError, ErrorCategory, action_result
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.SECURITY, message, recoverable=False,
    ))
    r.data = {"result": code, "message": message, "public_side_effect": False}
    return r


def degraded(code: str, message: str, normalized: str,
             posted_url: Optional[str] = None,
             posted_post_id: Optional[str] = None,
             target_post_id: Optional[str] = None) -> ActionResult:
    """Uncertain submit — public side effect possible. The public_side_effect
    flag drives the kernel's dedupe rule (uncertain counts as happened)."""
    from super_browser.results import ActionError, ErrorCategory, action_result
    r = action_result(ok=False, error=ActionError(
        ErrorCategory.UNKNOWN, message, recoverable=False,
    ))
    r.data = {
        "result": code, "message": message, "public_side_effect": True,
        "submitted_text": normalized,
        "posted_url": posted_url, "posted_post_id": posted_post_id,
        "target_post_id": target_post_id,
        "supports_compensation": False,
    }
    return r


async def _abort(broker: Any, code: str, message: str) -> ActionResult:
    """Abort and cleanup: close the composer, never submit a partial state."""
    try:
        await broker.close_composer()
    except Exception:  # noqa: BLE001
        pass
    return failure(code, message)


async def run_media_compose(
    broker: Any, spec: MediaComposeSpec, hooks: PostSubmitHooks,
) -> ActionResult:
    """Run the transaction. Returns the final ActionResult for failure and
    degraded paths; on success returns ok_result with `outcome` in data —
    the calling capability reshapes it into its own reporting vocabulary."""

    async def _fail_after_open(code: str, message: str) -> ActionResult:
        return await _abort(broker, code, message)

    # Gate 1: recompute every digest before any upload.
    from webwire.safety.attachment import file_sha256
    for item in spec.items:
        try:
            current = file_sha256(item["source_path"])
        except OSError as exc:
            return await _fail_after_open(
                "media_changed_after_confirmation",
                f"Cannot read {item['source_path']}: {exc!r}")
        if current != item["sha256"]:
            return await _fail_after_open(
                "media_changed_after_confirmation",
                f"Image {item['index']} changed since confirmation.")

    # Gate 2: target-context hook FIRST (M3a lesson) — context before media.
    if spec.target_post_id is not None:
        if spec.context == "quote":
            open_r = await broker.open_quote_on_target(
                spec.target_post_url, spec.target_post_id,
            )
            open_fail_code = "quote_action_not_available"
            fill = broker.fill_quote_composer
        else:
            open_r = await broker.open_reply_on_target(
                spec.target_post_url, spec.target_post_id,
            )
            open_fail_code = spec.target_open_failure_code
            fill = broker.fill_reply_composer
        if not open_r.ok:
            # Nothing opened — no composer to close, no media attached.
            return failure(open_fail_code,
                           f"Could not open {spec.context} on target: {open_r.error}")
        fill_r = await fill(spec.normalized_text)
        if not fill_r.ok:
            return await _fail_after_open(
                "pre_submit_mismatch",
                f"Could not fill {spec.context} composer: {fill_r.error}")
    else:
        fill_r = await broker.fill_composer(spec.normalized_text)
        if not fill_r.ok:
            return await _fail_after_open(
                "pre_submit_mismatch", f"Could not fill composer: {fill_r.error}")

    # Gate 3: ordered uploads with the exact-count gate after each item.
    for i, item in enumerate(spec.items):
        attach_r = await broker.attach_media(item["source_path"])
        if not attach_r.ok:
            return await _fail_after_open(
                "attachment_upload_failed",
                f"Image {item['index']} upload failed: {attach_r.error}")
        ready_r = await broker.verify_attachment_ready()
        if not ready_r.ok:
            return await _fail_after_open(
                "attachment_not_ready",
                f"Image {item['index']} not ready: {ready_r.error}")
        count_r = await broker.count_attachments()
        actual = (count_r.data or {}).get("count", 0) if count_r.ok else 0
        if actual != i + 1:
            return await _fail_after_open(
                "attachment_count_mismatch",
                f"Expected {i+1} attachments after upload {i+1}, got {actual}. "
                "Aborting — no partial post.")

    # Gate 4: composition re-verification (media picker may have mutated text).
    read_r = await broker.read_composer_text()
    if not read_r.ok:
        return await _fail_after_open("pre_submit_mismatch", "Could not read composer text.")
    composer_text = (read_r.data or {}).get("composer_text", "")
    if normalize_for_compare(composer_text) != normalize_for_compare(spec.normalized_text):
        return await _fail_after_open(
            "pre_submit_mismatch",
            "Composer text mismatch after all uploads. Aborting.")

    # Gate 5: final exact-count check.
    final_r = await broker.count_attachments()
    final_count = (final_r.data or {}).get("count", 0) if final_r.ok else 0
    if final_count != spec.expected_count:
        return await _fail_after_open(
            "attachment_count_mismatch",
            f"Final count {final_count} != expected {spec.expected_count}.")

    # Gate 6: pre-submit identity capture.
    pre_submit_ids = await hooks.capture_pre_submit_ids(broker)

    # Gate 7: final kill check — hand on the button.
    if hasattr(broker, "_kill") and broker._kill and broker._kill.tripped():
        return await _fail_after_open(
            "killed_before_submit", "Kill switch tripped. Aborting.")

    # Gate 8: submit.
    submit_r = await broker.click_submit()
    if not submit_r.ok:
        return degraded("submit_clicked_verification_pending",
                        "Submit clicked but result uncertain.",
                        spec.normalized_text,
                        target_post_id=spec.target_post_id)

    # Gate 9: identity-aware post-submit capture + verification.
    await asyncio.sleep(3)
    posted_post_id, posted_url = await hooks.capture_new_post_id(
        broker, pre_submit_ids, exclude_ids=set(spec.exclude_ids),
    )
    if not posted_post_id or not posted_url:
        return degraded("submit_clicked_verification_pending",
                        "Submit clicked but no new post ID captured.",
                        spec.normalized_text,
                        target_post_id=spec.target_post_id)
    await asyncio.sleep(3)
    text_ok = await hooks.verify_text(broker, posted_url, spec.normalized_text)
    media_count = await hooks.count_media(broker, posted_url)

    outcome = ComposeOutcome(
        posted=True, code="captured", message="",
        posted_url=posted_url, posted_post_id=posted_post_id,
        text_verified=text_ok, media_count=media_count,
    )
    return ok_result(data={
        "outcome": {
            "posted_url": outcome.posted_url,
            "posted_post_id": outcome.posted_post_id,
            "text_verified": outcome.text_verified,
            "media_count": outcome.media_count,
        },
    })
