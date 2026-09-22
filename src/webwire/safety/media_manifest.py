"""Ordered media manifest for multi-image posts (v0.2 M4a).

ChatGPT's M4 framework: treat multiple images as one ordered media-manifest
transaction, not as repeated single-image operations.

Each manifest item binds:
- media_index (immutable order)
- canonical source path
- source digest (SHA-256)
- validated MIME type
- dimensions
- alt text

The manifest order is immutable from preview through verification. One invalid
item rejects the ENTIRE invocation — do not begin uploading a valid prefix.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from webwire.safety.attachment import (
    Attachment,
    MediaValidationError,
    validate_media_file,
)

logger = logging.getLogger(__name__)

__all__ = ["MediaManifest", "MediaManifestItem", "MAX_IMAGES_PER_POST"]

# X's platform limit for images per post.
MAX_IMAGES_PER_POST = 4


@dataclass(frozen=True)
class MediaManifestItem:
    """One item in an ordered media manifest."""
    index: int                         # 0-based, immutable order
    attachment: Attachment             # validated attachment (path, sha256, mime, etc.)

    @property
    def source_path(self) -> str:
        return self.attachment.path

    @property
    def sha256(self) -> str:
        return self.attachment.sha256

    def to_preview_dict(self) -> dict:
        d = self.attachment.to_preview_dict()
        d["index"] = self.index
        return d


@dataclass(frozen=True)
class MediaManifest:
    """Immutable ordered collection of validated media attachments.

    Created by preflight_validation(). Once created, it never changes —
    the same manifest is used for preview, confirmation binding, upload,
    and post-submit verification.

    ChatGPT: 'One invalid item rejects the entire invocation. Do not begin
    uploading a valid prefix.'
    """
    items: tuple[MediaManifestItem, ...]

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self):
        return iter(self.items)

    @property
    def count(self) -> int:
        return len(self.items)

    @property
    def sha256_list(self) -> list[str]:
        """Ordered list of SHA-256 digests — for dedupe key + confirmation binding."""
        return [item.sha256 for item in self.items]

    @property
    def combined_hash(self) -> str:
        """Deterministic hash of all attachment digests in order — for dedupe."""
        import hashlib
        combined = "|".join(self.sha256_list)
        return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]

    def to_preview_list(self) -> list[dict]:
        return [item.to_preview_dict() for item in self.items]


def preflight_manifest(
    image_paths: list[str | Path],
    upload_roots: list[Path] | None = None,
    alt_texts: list[str | None] | None = None,
) -> MediaManifest:
    """Validate ALL images before ANY upload (ChatGPT's M4 gate #1).

    One invalid item rejects the entire invocation. Returns an immutable
    MediaManifest ready for preview → confirmation → upload → verification.

    Raises MediaValidationError on any item failure.
    """
    if not image_paths:
        raise MediaValidationError("empty_manifest", "No images provided.")

    if len(image_paths) > MAX_IMAGES_PER_POST:
        raise MediaValidationError(
            "too_many_images",
            f"X allows max {MAX_IMAGES_PER_POST} images per post; got {len(image_paths)}.",
        )

    # Pad alt_texts to match image_paths length.
    if alt_texts is None:
        alt_texts = [None] * len(image_paths)
    elif len(alt_texts) < len(image_paths):
        alt_texts = list(alt_texts) + [None] * (len(image_paths) - len(alt_texts))

    items: list[MediaManifestItem] = []
    seen_digests: set[str] = set()

    for i, (path, alt_text) in enumerate(zip(image_paths, alt_texts, strict=False)):
        attachment = validate_media_file(path, upload_roots=upload_roots, alt_text=alt_text)

        # Duplicate-item policy (ChatGPT: no silent deduplication, but reject exact duplicates).
        if attachment.sha256 in seen_digests:
            raise MediaValidationError(
                "duplicate_image",
                f"Image {i} ({attachment.basename}) is identical to a previous image "
                f"(same SHA-256: {attachment.sha256[:12]}).",
            )
        seen_digests.add(attachment.sha256)

        items.append(MediaManifestItem(index=i, attachment=attachment))

    return MediaManifest(items=tuple(items))
