"""download_image capability — download a tweet's image to local filesystem (M2).

READ-tier capability (no confirmation needed), but uses the separate
DownloadBroker (local-output boundary) — NOT the ReadOnlyBroker — because
downloading writes to the filesystem.

ChatGPT's directive: 'Image download is not truly read-only. It writes to the
local filesystem. Introduce a separate local-output boundary.'
"""

from __future__ import annotations

import logging
from typing import Any

from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.capabilities.base import CapabilityTier
from webwire.envelope import ActionResult, ok_result, soft_failure

logger = logging.getLogger(__name__)

__all__ = ["DownloadImageCapability"]


class DownloadImageCapability:
    """Download the first image from a post to the local download directory."""

    name = "download_image"
    tier = CapabilityTier.READ

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        post_url = input.get("post_url") or input.get("url")
        if not post_url:
            return soft_failure(
                "download_image requires 'post_url' input.",
                failure_category=FailureCategory.VALIDATION,
            )

        # The DownloadBroker is passed as 'broker' by the dispatcher for this
        # capability. It's NOT the ReadOnlyBroker — it's the separate
        # local-output boundary.
        if not hasattr(broker, "resolve_post_image_url"):
            return soft_failure(
                "download_image requires a DownloadBroker.",
                failure_category=FailureCategory.SECURITY,
            )

        # Step 1: resolve the image URL from the post.
        resolve_r = await broker.resolve_post_image_url(post_url)
        if not resolve_r.ok:
            return resolve_r
        image_url = resolve_r.data.get("url")
        image_id = resolve_r.data.get("image_id")
        alt_text = resolve_r.data.get("alt")

        if not image_url:
            return soft_failure(
                "No image found in this post.",
                failure_category=FailureCategory.SELECTOR_NOT_FOUND,
            )

        # Step 2: download the image.
        dl_r = await broker.download_image(image_url, image_id)
        if not dl_r.ok:
            return dl_r

        return ok_result(
            data={
                "downloaded": True,
                "path": dl_r.data.get("path"),
                "filename": dl_r.data.get("filename"),
                "size_bytes": dl_r.data.get("size_bytes"),
                "source_url": dl_r.data.get("source_url"),
                "image_id": image_id,
                "alt_text": alt_text,
                "post_url": post_url,
            },
            success_category=SuccessCategory.ARTIFACT,
        )
