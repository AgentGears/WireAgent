"""Read-only M5 evidence surface coordinated with the browser write lease.

Layer 5 must not let verification navigate the browser concurrently with an M5
writer that owns transient DOM provenance. This adapter reuses the exact
``M5LeasedWriteBroker`` state while exposing evidence operations only; it has no
mutation methods and never hands capability code the underlying browser facade.

Two lease modes are intentional:
- pre-submit baseline capture runs under the *owned content* lease because the
  approved composer is still open;
- post-submit capture/verification runs as a one-shot read after successful
  permit consumption releases the content owner.
"""

from __future__ import annotations

import json

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_write_broker import M5WriteBroker
from webwire.safety.media_verify import verify_post_text
from webwire.safety.post_submit import capture_new_post_id

__all__ = ["M5LeasedEvidenceReader"]


class M5LeasedEvidenceReader:
    """Lease-coordinated evidence for migrated M5 effects."""

    __slots__ = ("__broker",)

    def __init__(self, broker: M5LeasedWriteBroker) -> None:
        if not isinstance(broker, M5LeasedWriteBroker):
            raise TypeError("M5 evidence reader requires M5LeasedWriteBroker")
        self.__broker = broker

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        broker = self.__broker
        return await broker._one_shot(
            lambda: M5WriteBroker.read_bookmark_state(broker, post_url)
        )

    async def read_like_state(self, post_url: str) -> ActionResult:
        broker = self.__broker
        return await broker._one_shot(
            lambda: M5WriteBroker.read_like_state(broker, post_url)
        )

    async def capture_pre_submit_ids(self) -> ActionResult:
        """Capture visible status IDs without relinquishing composer ownership.

        Unlike the historical helper, this path does not coerce CDP/evaluation
        failure to an empty set: an empty baseline can be legitimate, so error
        and emptiness must remain distinct safety facts.
        """
        broker = self.__broker

        async def operation() -> ActionResult:
            expr = (
                '(function(){'
                'var links=document.querySelectorAll("a[href*=\'/status/\']");'
                'var ids={};'
                'for(var i=0;i<links.length;i++){'
                'var href=links[i].getAttribute("href")||"";'
                'var m=href.match(/\\/status\\/(\\d+)/);'
                'if(m)ids[m[1]]=1;'
                '}'
                'return JSON.stringify(Object.keys(ids));'
                '})()'
            )
            try:
                result = await broker._sb._controller._cdp.evaluate(expr)
            except Exception as exc:  # noqa: BLE001
                return soft_failure(
                    f"pre-submit identity baseline evaluation failed: {exc!r}",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not result.ok or not result.data or "exceptionDetails" in result.data:
                return soft_failure(
                    "pre-submit identity baseline evaluation did not return evidence",
                    failure_category=FailureCategory.UNKNOWN,
                )
            raw = result.data.get("result", {}).get("value")
            if not isinstance(raw, str):
                return soft_failure(
                    "pre-submit identity baseline returned malformed evidence",
                    failure_category=FailureCategory.UNKNOWN,
                )
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError) as exc:
                return soft_failure(
                    f"pre-submit identity baseline was not valid JSON: {exc!r}",
                    failure_category=FailureCategory.UNKNOWN,
                )
            if not isinstance(parsed, list) or not all(
                isinstance(value, str) for value in parsed
            ):
                return soft_failure(
                    "pre-submit identity baseline had invalid status IDs",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"status_ids": sorted(set(parsed))})

        return await broker._owned_content(operation)

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult:
        """Find a post that appeared after the approved submit boundary."""
        broker = self.__broker

        async def operation() -> ActionResult:
            post_id, post_url = await capture_new_post_id(
                broker,
                set(pre_submit_ids),
                exclude_ids=set(exclude_ids or ()),
            )
            if not post_id or not post_url:
                return soft_failure(
                    "post-submit evidence did not identify a new status",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(data={"post_id": post_id, "post_url": post_url})

        return await broker._one_shot(operation)

    async def verify_post_text(self, post_url: str, normalized_text: str) -> ActionResult:
        """Verify the exact posted status text under the shared browser lease."""
        broker = self.__broker

        async def operation() -> ActionResult:
            matched = await verify_post_text(broker, post_url, normalized_text)
            if not matched:
                return soft_failure(
                    "post-submit text evidence did not match the approved text",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(
                data={
                    "text_matches": True,
                    "post_url": post_url,
                }
            )

        return await broker._one_shot(operation)
