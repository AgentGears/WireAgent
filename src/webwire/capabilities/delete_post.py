"""delete_post capability — remove one of the user's own posts (2026-09-23).

The compensation made real: the risk registry has referenced
`delete_post` / `delete_reply` / `delete_quote` as compensation actions
since Phase 0b; this capability is the executable behind those labels
(posts, replies, and quotes are all statuses — one capability serves all).

Semantics:
- IRREVERSIBLE (conservative tier by derivation — recreation is a new post;
  thread replies and quotes of a deleted post break; copies persist).
- Id-scoped: the caret menu opens on the article whose status href matches
  the target id — never "the first article" (the M4b lesson).
- Two-phase confirmation like every write; kill re-checked by the broker
  immediately before the confirm click (invariant 12).
- Honest verification: post-state vocabulary 'present' | 'deleted' |
  'unknown'; verify ok requires 'deleted'.

Result codes:
- post_deleted_and_verified / post_deleted_state_unknown
- target_not_found (already deleted / not yours / bad URL)
- menu_delete_unavailable / confirmation_failed (cleanup ran)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["DeletePostCapability"]


class DeletePostCapability:
    """Delete one of the user's own posts. PUBLIC_CONTENT_IRREVERSIBLE
    (deletion cannot be undone — recreation is a new post)."""

    name = "delete_post"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        post_url = input.get("post_url") or ""
        target_post_id = input.get("target_post_id") or ""
        if not target_post_id and post_url:
            m = re.search(r"/status/(\d+)", post_url)
            target_post_id = m.group(1) if m else post_url
        meta, comp = DEFAULT_REGISTRY.get("delete_post")
        return WriteIntent(
            action_type="delete_post",
            target_type="post",
            target_id=str(target_post_id),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
            payload={"post_url": post_url, "target_post_id": str(target_post_id)},
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Read-only preview via the ReadOnlyBroker: check the post exists
        so a delete of a nonexistent target is caught before confirmation."""
        post_url = intent.payload.get("post_url") or (
            f"https://x.com/i/status/{intent.target_id}"
        )
        state = "unknown"
        try:
            # Existence probe via the broker's bounded round-trip — POLLED
            # (the shell-before-feed lesson applies to permalinks too: a
            # single-shot probe fired pre-hydration and reported a
            # just-created post as absent, live-caught 2026-09-23).
            if hasattr(broker, "navigate"):
                nav = await broker.navigate(post_url)
                if nav.ok and hasattr(broker, "probe_selectors"):
                    import asyncio
                    for _ in range(4):
                        pr = await broker.probe_selectors({
                            "target_article": [f"a[href*='/status/{intent.target_id}']"],
                        })
                        if pr.ok and (pr.data or {}).get("probes", {}).get("target_article"):
                            state = "present"
                            break
                        await asyncio.sleep(1.0)
                    else:
                        state = "absent"
        except Exception:  # noqa: BLE001 — preview must never raise
            state = "unknown"

        warnings = [
            "IRREVERSIBLE: deletion cannot be undone. Recreation is a NEW "
            "post; replies and quotes of the deleted post break; copies, "
            "screenshots, and caches may persist.",
        ]
        if state == "absent":
            warnings.insert(0, "TARGET NOT FOUND: the post appears absent "
                               "(already deleted, not yours, or bad URL).")
        return PreviewResult(
            summary=f"Will delete post {intent.target_id}",
            target_url=post_url,
            current_state=f"post state: {state}",
            warnings=warnings,
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Delete via the WriteBroker's semantic delete_post method."""
        post_url = intent.payload.get("post_url") or (
            f"https://x.com/i/status/{intent.target_id}"
        )
        if not hasattr(broker, "delete_post"):
            return soft_failure(
                "delete_post execute requires a broker with delete_post()",
                failure_category=FailureCategory.SECURITY,
            )
        r = await broker.delete_post(post_url, intent.target_id)
        if r.ok:
            data = r.data or {}
            data["result"] = "post_deleted"
            data["target_post_id"] = intent.target_id
            data["write_tier"] = "public_content_irreversible"
            data["supports_compensation"] = False
            data["residual_side_effects"] = [
                "deletion_is_not_undo",
                "replies_and_quotes_of_target_break",
                "copies_screenshots_caches_may_persist",
            ]
        return r

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Verify the post is gone. Honest-envelope rule: ok=True means
        VERIFIED — post_state must read 'deleted'. 'present' and 'unknown'
        are verification failures with the observed state in the message."""
        post_url = intent.payload.get("post_url") or (
            f"https://x.com/i/status/{intent.target_id}"
        )
        if not hasattr(broker, "read_post_state"):
            return soft_failure("delete verify requires read_post_state()")
        r = await broker.read_post_state(post_url, intent.target_id)
        if r.ok:
            state = (r.data or {}).get("post_state")
            if state != "deleted":
                return soft_failure(
                    f"delete verify could not confirm (post_state={state!r}) "
                    f"for post {intent.target_id}",
                    failure_category=FailureCategory.UNKNOWN,
                )
        return r
