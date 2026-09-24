"""Read-only M5 evidence surface coordinated with the browser write lease.

Layer 5 must not let post-effect verification navigate the browser concurrently
with an M5 writer that owns transient DOM provenance. This adapter reuses the
same per-browser lease state as ``M5LeasedWriteBroker`` while exposing only the
state reads required by the initial bookmark/like migration.

The class is intentionally narrow. It has no mutation methods and does not hand
capability code the underlying broker or browser facade.
"""

from __future__ import annotations

from webwire.envelope import ActionResult
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.m5_write_broker import M5WriteBroker

__all__ = ["M5LeasedEvidenceReader"]


class M5LeasedEvidenceReader:
    """Lease-coordinated readback for migrated engagement effects."""

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
