"""Supported Layer-5 construction path for M5 scoped authority.

The generic :class:`ScopedAuthorityBroker` remains useful for unit-test doubles
and isolated adapter testing. Live migration must use this factory so an older
provenance-only broker cannot accidentally bypass the per-browser M5 write
lease required by Layer 4's transient DOM ownership contract.

The live factory deliberately does not attach WireAgent state directly to the
third-party SuperBrowser facade. Some SDK facade implementations may use slots
or otherwise reject arbitrary attributes. WireAgent instead keeps one stable
process-local proxy for each exact facade identity; the leased broker attaches
its state to that owned proxy. Repeated construction for the same facade reuses
the same proxy and therefore the same browser-write lease.

The write broker and CommitGateway must also share the exact same KillSwitch
instance. This makes the operator's in-process trip state one authority source
for reversible staging and the final gateway transition rather than relying on
two objects merely happening to point at the same hot-file path.
"""

from __future__ import annotations

import threading
from typing import Any

from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES, EffectPolicyRegistry
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied

__all__ = [
    "build_live_m5_write_broker",
    "build_live_scoped_authority_broker",
]

_REQUIRED_SCOPED_AUTHORITY_VERSION = 3
_LIVE_FACADE_LOCK = threading.RLock()
_LIVE_FACADES: dict[int, tuple[Any, "_LeaseCompatibleBrowserFacade"]] = {}


class _LeaseCompatibleBrowserFacade:
    """WireAgent-owned attribute-capable proxy for one exact SDK facade."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _live_facade(super_browser: Any) -> _LeaseCompatibleBrowserFacade:
    """Return one stable proxy for an exact process-local browser facade.

    The registry intentionally holds a strong reference for process lifetime.
    Browser/session lifecycle is already process-owned by WireAgent; avoiding an
    SDK-mutability assumption is more important than reclaiming this tiny entry
    before process shutdown. Identity is checked explicitly so an ``id`` value
    can never be reused for a different live object while the entry exists.
    """

    key = id(super_browser)
    with _LIVE_FACADE_LOCK:
        existing = _LIVE_FACADES.get(key)
        if existing is not None:
            owner, facade = existing
            if owner is super_browser:
                return facade
            raise RuntimeError("live browser facade identity registry collision")
        facade = _LeaseCompatibleBrowserFacade(super_browser)
        _LIVE_FACADES[key] = (super_browser, facade)
        return facade


def build_live_m5_write_broker(
    super_browser: Any,
    kill_switch: KillSwitch,
) -> M5LeasedWriteBroker:
    """Build the supported live v3 broker without mutating the SDK facade."""

    return M5LeasedWriteBroker(_live_facade(super_browser), kill_switch)


def build_live_scoped_authority_broker(
    write_broker: Any,
    commit_gateway: CommitGateway,
    *,
    policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
) -> ScopedAuthorityBroker:
    """Build the only supported live Layer-5 scoped-authority adapter."""

    version = getattr(write_broker, "scoped_authority_version", 0)
    browser_facade = getattr(write_broker, "_sb", None)
    broker_kill = getattr(write_broker, "_kill", None)
    gateway_kill = getattr(commit_gateway, "_kill", None)
    if (
        not isinstance(write_broker, M5LeasedWriteBroker)
        or not isinstance(version, int)
        or version < _REQUIRED_SCOPED_AUTHORITY_VERSION
        or not isinstance(browser_facade, _LeaseCompatibleBrowserFacade)
        or broker_kill is not gateway_kill
    ):
        raise ScopedAuthorityDenied(
            "broker_contract_mismatch",
            "live M5 execution requires the WireAgent live factory, one shared "
            "KillSwitch, and M5LeasedWriteBroker contract version >= 3",
        )
    return ScopedAuthorityBroker(
        write_broker,
        commit_gateway,
        policies=policies,
    )
