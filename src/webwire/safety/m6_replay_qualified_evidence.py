"""Layer-7 lease-coordinated like evidence for the supported live stack.

Layer 5's generic evidence reader deliberately calls the base M5 like-state
reader. Layer 7 has stronger target/control authority semantics, so the live
stack must verify a consumed like permit through the exact same qualified state
reader used before mutation. Otherwise contradictory, hidden, nested, duplicate,
or disabled controls could be rejected at execution time but later accepted as
terminal evidence by the older reader.
"""

from __future__ import annotations

from webwire.envelope import ActionResult
from webwire.m6_replay_qualified_write_broker import M6ReplayQualifiedWriteBroker
from webwire.safety.m5_actor_bound_evidence import M5ActorBoundEvidenceReader

__all__ = ["M6ReplayQualifiedEvidenceReader"]


class M6ReplayQualifiedEvidenceReader(M5ActorBoundEvidenceReader):
    """Actor-bound evidence plus Layer-7 qualified like-state verification."""

    __slots__ = ("__layer7_broker",)

    def __init__(self, broker: M6ReplayQualifiedWriteBroker) -> None:
        if not isinstance(broker, M6ReplayQualifiedWriteBroker):
            raise TypeError(
                "Layer-7 replay-qualified evidence requires "
                "M6ReplayQualifiedWriteBroker"
            )
        super().__init__(broker)
        self.__layer7_broker = broker

    async def read_like_state(self, post_url: str) -> ActionResult:
        """Verify like state under the shared browser one-shot lease."""

        broker = self.__layer7_broker
        return await broker._one_shot(lambda: broker.read_like_state(post_url))
