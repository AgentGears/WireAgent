"""Lease-coordinated post-submit evidence for M5 media content.

The existing :class:`M5LeasedEvidenceReader` owns identity/text/target evidence.
This companion keeps media presence separate and honest: it proves only the
number of rendered attachments on the exact captured status. It does not claim
source-byte equivalence after platform transcoding.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.media_verify import count_post_media

__all__ = ["M5LeasedMediaEvidenceReader", "M5MediaCountEvidence"]


@runtime_checkable
class M5MediaCountEvidence(Protocol):
    async def count_post_media(self, post_url: str) -> ActionResult: ...


class M5LeasedMediaEvidenceReader:
    """Expose attachment-count evidence through the shared M5 browser lease."""

    __slots__ = ("__broker",)

    def __init__(self, broker: M5LeasedWriteBroker) -> None:
        if not isinstance(broker, M5LeasedWriteBroker):
            raise TypeError("M5 media evidence requires M5LeasedWriteBroker")
        self.__broker = broker

    async def count_post_media(self, post_url: str) -> ActionResult:
        broker = self.__broker

        async def operation() -> ActionResult:
            count = await count_post_media(broker, post_url)
            if not isinstance(count, int) or count <= 0:
                return soft_failure(
                    "post-submit media evidence did not establish any attachments",
                    failure_category=FailureCategory.UNKNOWN,
                )
            return ok_result(
                data={
                    "post_url": post_url,
                    "media_count": count,
                    "source_byte_equivalence_verified": False,
                }
            )

        return await broker._one_shot(operation)
