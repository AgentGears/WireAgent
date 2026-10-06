"""M7 Layer 5 — the bounded retained request table (frozen §10.5 / M7-RV04).

The owner-side dedupe table behind the IPC pipeline. The KEY is the
client's high-entropy ``request_id``; the stored comparison VALUE is the
canonical normalized request identity (which excludes ``request_id``,
``runtime_build_id``, and ``authority_instance_id`` — the Layer-4 rule,
retained so transport identity never masquerades as semantic equality).

Semantics (frozen):

    new id + canonical request          -> execute once
    same id + same in-flight request    -> join/await the SAME owner work
    same id + same completed request    -> return retained response
    same id + different request         -> protocol violation; no execution

In-flight entries are pinned and non-evictable: capacity pressure refuses
NEW admission (backpressure) rather than discarding live owner work.
Completed entries are process-local and bounded; after eviction there is
no transport-level exactly-once claim and eviction grants no replay
authority — M5/M6 confirmation/effect/recovery truth remains the safety
boundary.

The table is NOT thread-safe by contract: it is owned by the pipeline,
which runs on the owner's event loop. ``classify`` creates asyncio
futures against the running loop; joiners await the same future the
executor eventually resolves.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
from typing import Optional

__all__ = [
    "RetainedRequestTable",
    "TableDecision",
    "IPC_TABLE_MAX_INFLIGHT",
    "IPC_TABLE_MAX_COMPLETED",
]

IPC_TABLE_MAX_INFLIGHT = 64
IPC_TABLE_MAX_COMPLETED = 256

_KIND_NEW = "new"
_KIND_JOIN = "join"
_KIND_RETAINED = "retained"
_KIND_VIOLATION = "violation"
_KIND_FULL = "full"


@dataclass
class TableDecision:
    """The classification of one (request_id, canonical request) pair."""

    kind: str
    future: Optional[asyncio.Future] = None  # new/join: the owner work
    response: Optional[bytes] = None  # retained: the completed frame


@dataclass
class _Inflight:
    canonical: str
    future: "asyncio.Future[bytes]"


class RetainedRequestTable:
    """Process-local retained request state for ONE owner instance."""

    def __init__(
        self,
        *,
        max_inflight: int = IPC_TABLE_MAX_INFLIGHT,
        max_completed: int = IPC_TABLE_MAX_COMPLETED,
    ) -> None:
        if max_inflight < 1 or max_completed < 1:
            raise ValueError("table capacities must be >= 1")
        self._max_inflight = max_inflight
        self._max_completed = max_completed
        self._inflight: dict[str, _Inflight] = {}
        # Completed LRU: request_id -> (canonical, response frame). A
        # retained hit refreshes recency; insertion evicts the oldest.
        self._completed: OrderedDict[str, tuple[str, bytes]] = OrderedDict()

    # -- inspection ---------------------------------------------------------

    @property
    def inflight_count(self) -> int:
        return len(self._inflight)

    @property
    def completed_count(self) -> int:
        return len(self._completed)

    # -- classification (owner-loop thread only) ---------------------------

    def classify(self, request_id: str, canonical: str) -> TableDecision:
        """Classify one retained-request lookup.

        ``new`` registers the in-flight entry and returns its future —
        the executor MUST later call ``complete`` (terminal response
        frame) or ``abandon`` (infrastructure failure)."""
        inflight = self._inflight.get(request_id)
        if inflight is not None:
            if inflight.canonical == canonical:
                return TableDecision(kind=_KIND_JOIN, future=inflight.future)
            return TableDecision(kind=_KIND_VIOLATION)
        completed = self._completed.get(request_id)
        if completed is not None:
            completed_canonical, response = completed
            if completed_canonical == canonical:
                self._completed.move_to_end(request_id)  # LRU recency
                return TableDecision(kind=_KIND_RETAINED, response=response)
            return TableDecision(kind=_KIND_VIOLATION)
        # New id. In-flight entries are pinned and non-evictable, so
        # saturation refuses admission instead of discarding live work.
        if len(self._inflight) >= self._max_inflight:
            return TableDecision(kind=_KIND_FULL)
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._inflight[request_id] = _Inflight(canonical=canonical, future=future)
        return TableDecision(kind=_KIND_NEW, future=future)

    # -- executor terminal transitions (owner-loop thread only) ------------

    def complete(self, request_id: str, response: bytes) -> None:
        """The admitted request reached its terminal response frame (a
        capability error envelope is still a terminal outcome — the
        retry observes the same frame rather than re-executing)."""
        entry = self._inflight.pop(request_id, None)
        if entry is None:
            return
        if len(self._completed) >= self._max_completed:
            self._completed.popitem(last=False)  # evict the oldest completed
        self._completed[request_id] = (entry.canonical, response)
        if not entry.future.done():
            entry.future.set_result(response)

    def abandon(self, request_id: str, error: BaseException) -> None:
        """Infrastructure failure: the admitted request never produced a
        terminal frame. The slot frees and joiners observe the failure —
        a retry classifies as NEW again (no phantom retention)."""
        entry = self._inflight.pop(request_id, None)
        if entry is None:
            return
        if not entry.future.done():
            entry.future.set_exception(error)
