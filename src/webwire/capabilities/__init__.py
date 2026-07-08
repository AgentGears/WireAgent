"""Capability implementations."""

from webwire.capabilities.base import Capability, CapabilityTier
from webwire.capabilities.health import HealthCapability
from webwire.capabilities.whoami import WhoamiCapability

__all__ = ["Capability", "CapabilityTier", "WhoamiCapability", "HealthCapability"]
