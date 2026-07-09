"""bookmark_post capability — the first real write (Phase 3 canary).

A narrow vertical slice through the WriteKernel pipeline. Implements the
WriteCapability protocol (compose/preview/execute/verify). The kernel enforces
compose→preview→policy→confirm→execute→journal→verify; this capability supplies
only the bookmark-specific logic.

Per ChatGPT's Phase 3 directive: do NOT generalize the write DOM layer. This is
bookmark only; generalization comes after the first live write proves the kernel.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import CompensationMeta, DEFAULT_REGISTRY, RiskMeta, WriteIntent
from webwire.safety.write_kernel import PreviewResult, WriteCapability
from webwire.write_broker import WriteBroker

logger = logging.getLogger(__name__)

__all__ = ["BookmarkCapability"]


class BookmarkCapability:
    """Bookmark a post. PRIVATE_REVERSIBLE tier — the lowest-risk write."""

    name = "bookmark_post"

    # Import tier lazily to avoid circular import at module level.
    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        """Produce a WriteIntent for bookmarking. NO browser interaction."""
        post_id = input.get("post_id")
        post_url = input.get("post_url") or ""
        # Derive post_id from URL if not given directly.
        if not post_id and post_url:
            import re
            m = re.search(r"/status/(\d+)", post_url)
            post_id = m.group(1) if m else post_url
        meta, comp = DEFAULT_REGISTRY.get("bookmark")
        return WriteIntent(
            action_type="bookmark",
            target_type="post",
            target_id=str(post_id),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
            payload={"post_url": post_url, "post_id": str(post_id)},
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Read-only preview. The broker here is a ReadOnlyBroker (preview runs
        before confirmation, so it uses the read-only surface)."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        return PreviewResult(
            summary=f"Will bookmark post {intent.target_id}",
            target_url=post_url,
            current_state="not bookmarked (will be bookmarked)",
            warnings=[],
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Execute the bookmark. The broker here is a WriteBroker (only reachable
        after the kernel's confirmation gate). Duck-typed: accepts any object
        with click_bookmark(). The kernel guarantees the right broker type."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        if not hasattr(broker, "click_bookmark"):
            return soft_failure(
                "bookmark execute requires a broker with click_bookmark()",
                failure_category=FailureCategory.SECURITY,
            )
        return await broker.click_bookmark(post_url)

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Verify the bookmark took effect. Duck-typed."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        if not hasattr(broker, "read_bookmark_state"):
            return soft_failure(
                "bookmark verify requires a broker with read_bookmark_state()",
                failure_category=FailureCategory.SECURITY,
            )
        return await broker.read_bookmark_state(post_url)
