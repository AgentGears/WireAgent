"""like_post capability — the second write (Phase 3b public-engagement canary).

Key new design (ChatGPT's Phase 3b review): pre-existing-state handling.
- Case A (pre_state=not_liked): click like → verify → compensation eligible (unlike)
- Case B (pre_state=already_liked): NO click → already_satisfied → NO compensation
  (this invocation didn't create the like; must not undo it)

The principle: compensation reverses THIS invocation's delta, not merely the
final state. Carried forward to all future write capabilities.

Uses the LikeWritePort (narrow port protocol) — only sees click_like + read_like_state.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.write_kernel import PreviewResult, StateTransition, WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["LikeCapability"]


class LikeCapability:
    """Like a post. PUBLIC_REVERSIBLE_ENGAGEMENT tier."""

    name = "like_post"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        post_id = input.get("post_id")
        post_url = input.get("post_url") or ""
        if not post_id and post_url:
            m = re.search(r"/status/(\d+)", post_url)
            post_id = m.group(1) if m else post_url
        meta, comp = DEFAULT_REGISTRY.get("like")
        return WriteIntent(
            action_type="like",
            target_type="post",
            target_id=str(post_id),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
            payload={"post_url": post_url, "post_id": str(post_id)},
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Read-only preview. Checks the CURRENT like state so the caller knows
        whether this will be a real mutation or an already_satisfied no-op."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        # Read current like state if the broker supports it (WriteBroker in
        # execute context; for preview we may only have ReadOnlyBroker, which
        # doesn't have read_like_state — so preview is best-effort on state).
        current = "unknown"
        if hasattr(broker, "read_like_state"):
            try:
                state_r = await broker.read_like_state(post_url)
                if state_r.ok and state_r.data:
                    current = state_r.data.get("like_state", "unknown")
            except Exception:  # noqa: BLE001
                pass
        return PreviewResult(
            summary=f"Will like post {intent.target_id} (current: {current})",
            target_url=post_url,
            current_state=current,
            warnings=(
                ["Public engagement: like is visible to others and may notify the author"]
                if current != "liked" else
                ["Already liked — no action needed"]
            ),
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Execute the like with pre-existing-state handling."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        if not hasattr(broker, "click_like"):
            return soft_failure(
                "like execute requires a broker with click_like()",
                failure_category=FailureCategory.SECURITY,
            )

        # Read pre-state BEFORE executing (Case A vs Case B distinction).
        pre_state = "unknown"
        if hasattr(broker, "read_like_state"):
            try:
                pre_r = await broker.read_like_state(post_url)
                if pre_r.ok and pre_r.data:
                    pre_state = pre_r.data.get("like_state", "unknown")
            except Exception:  # noqa: BLE001
                pass

        transition = StateTransition(
            pre_state=pre_state,
            intended_state="liked",
            compensation_action=intent.compensation.compensation_action,
            residual_side_effects=list(intent.risk_meta.residual_side_effects),
        )

        # Case B: already liked → no-op, no compensation.
        if pre_state == "liked":
            transition.post_state = "liked"
            transition.changed_by_this_invocation = False
            transition.compensation_eligible = False
            return ok_result(data={
                "liked": True,
                "result": "already_satisfied",
                "state_transition": _transition_dict(transition),
            })

        # Case A: not liked → click like.
        click_r = await broker.click_like(post_url)
        transition.post_state = "liked" if click_r.ok else "unknown"
        transition.changed_by_this_invocation = click_r.ok
        transition.compensation_eligible = click_r.ok  # only if WE created the delta

        result_data = click_r.data or {}
        result_data["state_transition"] = _transition_dict(transition)
        return ok_result(data=result_data) if click_r.ok else click_r

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Verify the like state."""
        post_url = intent.payload.get("post_url") or f"https://x.com/i/status/{intent.target_id}"
        if not hasattr(broker, "read_like_state"):
            return soft_failure("like verify requires read_like_state()")
        return await broker.read_like_state(post_url)


def _transition_dict(t: StateTransition) -> dict:
    from dataclasses import asdict
    return asdict(t)
