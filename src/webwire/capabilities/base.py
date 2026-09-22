"""Capability protocol — the contract every capability satisfies.

Per the design principle "capability contracts, not commands": each capability
is a small object with a ``name``, a ``run`` coroutine, and a declared
read/write tier. Phase 0a registers only READ capabilities; the registry
physically cannot contain write capabilities because none are implemented.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from webwire.envelope import ActionResult


class CapabilityTier(StrEnum):
    """Declared blast-radius tier of a capability.

    Phase 0a allows READ only. WRITE is defined here for forward compatibility
    so the registry can reject write capabilities with a clear message if one
    is ever accidentally added before Phase 0b/3.
    """
    READ = "read"
    WRITE = "write"
    ANALYTICS = "analytics"


@runtime_checkable
class Capability(Protocol):
    """A capability contract (READ tier shape).

    Capabilities receive only a :class:`ReadOnlyBroker` (never the raw facade)
    plus arbitrary input. They return an :class:`ActionResult`. WRITE-tier
    capabilities satisfy the WriteCapability protocol instead (compose/
    preview/execute/verify) — both are registrable; see CapabilityRegistry.
    """

    name: str

    @property
    def tier(self) -> CapabilityTier: ...

    async def run(self, broker: Any, input: dict[str, Any]) -> ActionResult:
        ...


__all__ = ["Capability", "CapabilityTier"]
