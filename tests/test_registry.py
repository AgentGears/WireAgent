"""Tests for the capability registry — tier gating and name collision."""

from __future__ import annotations

import pytest

from webwire.capabilities.base import Capability, CapabilityTier
from webwire.registry import CapabilityRegistry


class FakeReadCap:
    name = "fake_read"
    tier = CapabilityTier.READ

    async def run(self, broker, input):  # type: ignore[no-untyped-def]
        ...


class FakeWriteCap:
    name = "fake_write"
    tier = CapabilityTier.WRITE

    async def run(self, broker, input):  # type: ignore[no-untyped-def]
        ...


def test_registry_accepts_read_capability() -> None:
    reg = CapabilityRegistry()
    reg.register(FakeReadCap())
    assert "fake_read" in reg
    assert reg.names() == ["fake_read"]


def test_registry_rejects_write_capability() -> None:
    """Phase 0a physically prevents write capabilities from registering."""
    reg = CapabilityRegistry()
    with pytest.raises(PermissionError, match="READ"):
        reg.register(FakeWriteCap())


def test_registry_rejects_duplicate_name() -> None:
    reg = CapabilityRegistry()
    reg.register(FakeReadCap())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FakeReadCap())


def test_registry_get_missing_returns_none() -> None:
    reg = CapabilityRegistry()
    assert reg.get("nope") is None
    assert len(reg) == 0
