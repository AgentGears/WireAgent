"""Supported Layer-5 construction path for M5 scoped authority.

The generic :class:`ScopedAuthorityBroker` remains useful for unit-test doubles
and isolated adapter testing. Live migration must use this factory so an older
provenance-only broker cannot accidentally bypass the per-browser M5 write
lease required by Layer 4's transient DOM ownership contract.
"""

from __future__ import annotations

from typing import Any

from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectPolicyRegistry
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied

__all__ = ["build_live_scoped_authority_broker"]

_REQUIRED_SCOPED_AUTHORITY_VERSION = 3


def build_live_scoped_authority_broker(
    write_broker: Any,
    commit_gateway: CommitGateway,
    *,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
) -> ScopedAuthorityBroker:
    """Build the only supported live Layer-5 scoped-authority adapter.

    Version 3 is the first broker contract that carries the per-SuperBrowser M5
    write lease plus single active content-context ownership. Older M5 broker
    seams are deliberately rejected for live migration.
    """
    version = getattr(write_broker, "scoped_authority_version", 0)
    if not isinstance(version, int) or version < _REQUIRED_SCOPED_AUTHORITY_VERSION:
        raise ScopedAuthorityDenied(
            "broker_contract_mismatch",
            "live M5 execution requires scoped_authority_version >= 3",
        )
    return ScopedAuthorityBroker(
        write_broker,
        commit_gateway,
        policies=policies,
    )
