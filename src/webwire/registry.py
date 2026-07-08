"""Capability registry — maps names to capability instances.

Phase 0a registers only READ capabilities. WRITE/ANALYTICS capabilities are
not merely disabled — they are physically absent (no class implements them).
The registry rejects any future write capability registration until Phase 0b/3
explicitly enables the write tier.
"""

from __future__ import annotations

import logging
from typing import Optional

from webwire.capabilities.base import Capability, CapabilityTier
from webwire.envelope import unsupported_capability

logger = logging.getLogger(__name__)

__all__ = ["CapabilityRegistry"]


class CapabilityRegistry:
    """Name -> Capability map with tier gating."""

    # Which tiers this build allows to register. Phase 0a = READ only.
    _ALLOWED_TIERS = frozenset({CapabilityTier.READ})

    def __init__(self) -> None:
        self._caps: dict[str, Capability] = {}

    def register(self, cap: Capability) -> None:
        """Register a capability. Raises if tier not allowed in this phase."""
        if cap.tier not in self._ALLOWED_TIERS:
            raise PermissionError(
                f"Capability {cap.name!r} has tier {cap.tier.value!r}, but only "
                f"READ capabilities may be registered in Phase 0a. "
                f"Write-safety kernel lands in Phase 0b."
            )
        if cap.name in self._caps:
            raise ValueError(f"Capability {cap.name!r} already registered")
        self._caps[cap.name] = cap
        logger.info("Registered capability %r (tier=%s)", cap.name, cap.tier.value)

    def get(self, name: str) -> Optional[Capability]:
        return self._caps.get(name)

    def names(self) -> list[str]:
        return sorted(self._caps.keys())

    def __contains__(self, name: object) -> bool:
        return name in self._caps

    def __len__(self) -> int:
        return len(self._caps)
