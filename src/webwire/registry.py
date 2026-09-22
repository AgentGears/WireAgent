"""Capability registry — maps names to capability instances.

Phase 0a registers only READ capabilities. WRITE/ANALYTICS capabilities are
not merely disabled — they are physically absent (no class implements them).
The registry rejects any future write capability registration until Phase 0b/3
explicitly enables the write tier.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Optional

from webwire.capabilities.base import Capability, CapabilityTier

if TYPE_CHECKING:
    from webwire.safety.write_kernel import WriteCapability

logger = logging.getLogger(__name__)

__all__ = ["CapabilityRegistry"]


class CapabilityRegistry:
    """Name -> Capability map with tier gating."""

    # Phase 0b: WRITE capabilities may now register (routed through the
    # WriteKernel by the dispatcher). READ + WRITE + ANALYTICS allowed.
    _ALLOWED_TIERS = frozenset({CapabilityTier.READ, CapabilityTier.WRITE, CapabilityTier.ANALYTICS})

    def __init__(self) -> None:
        self._caps: dict[str, "Capability | WriteCapability"] = {}

    def register(self, cap: "Capability | WriteCapability") -> None:
        """Register a capability (READ shape: run(); WRITE shape:
        compose/preview/execute/verify — routed by the dispatcher through the
        WriteKernel). Raises if tier not allowed."""
        if cap.tier not in self._ALLOWED_TIERS:
            tier_name = cap.tier.value if hasattr(cap.tier, "value") else str(cap.tier)
            raise PermissionError(
                f"Capability {cap.name!r} has tier {tier_name!r}, which is "
                f"not in the allowed tiers {sorted(t.value for t in self._ALLOWED_TIERS)}."
            )
        if cap.name in self._caps:
            raise ValueError(f"Capability {cap.name!r} already registered")
        self._caps[cap.name] = cap
        logger.info("Registered capability %r (tier=%s)", cap.name, cap.tier.value)

    def get(self, name: str) -> "Optional[Capability | WriteCapability]":
        return self._caps.get(name)

    def names(self) -> list[str]:
        return sorted(self._caps.keys())

    def __contains__(self, name: object) -> bool:
        return name in self._caps

    def __len__(self) -> int:
        return len(self._caps)
