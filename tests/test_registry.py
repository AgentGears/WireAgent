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


def test_registry_accepts_write_capability_phase0b() -> None:
    """Phase 0b: WRITE capabilities may now register (routed through the
    WriteKernel by the dispatcher). Previously (Phase 0a) they were rejected."""
    reg = CapabilityRegistry()
    reg.register(FakeWriteCap())  # should NOT raise
    assert "fake_write" in reg


def test_registry_rejects_unknown_tier() -> None:
    """Tiers not in the allowed set are still rejected."""

    class FakeUnknownCap:
        name = "fake_unknown"
        tier = "bogus_tier"  # not a valid CapabilityTier

        async def run(self, broker, input):  # type: ignore[no-untyped-def]
            ...

    reg = CapabilityRegistry()
    with pytest.raises(PermissionError):
        reg.register(FakeUnknownCap())  # type: ignore[arg-type]


def test_registry_rejects_duplicate_name() -> None:
    reg = CapabilityRegistry()
    reg.register(FakeReadCap())
    with pytest.raises(ValueError, match="already registered"):
        reg.register(FakeReadCap())


def test_registry_get_missing_returns_none() -> None:
    reg = CapabilityRegistry()
    assert reg.get("nope") is None
    assert len(reg) == 0
