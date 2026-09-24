"""Live-construction guard for M5 Layer-4 authority."""

from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.m5_leased_write_broker import M5LeasedWriteBroker
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_authority_factory import (
    build_live_m5_write_broker,
    build_live_scoped_authority_broker,
)
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied


class _OldBroker:
    scoped_authority_version = 2


class _SB:
    pass


class _SlottedSB:
    """SDK-like facade that deliberately rejects arbitrary attributes."""

    __slots__ = ()


def _kill(tmp_path: Path) -> KillSwitch:
    return KillSwitch(WebWireConfig(state_dir=tmp_path, kill_env_var=None))


def _gateway(tmp_path: Path, kill: KillSwitch | None = None) -> CommitGateway:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill or KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
    )


def test_live_factory_rejects_provenance_only_broker(tmp_path: Path) -> None:
    with pytest.raises(ScopedAuthorityDenied, match="broker_contract_mismatch"):
        build_live_scoped_authority_broker(_OldBroker(), _gateway(tmp_path))


def test_live_factory_rejects_direct_leased_broker(tmp_path: Path) -> None:
    """The supported path must not rely on mutating an arbitrary SDK facade."""

    kill = _kill(tmp_path)
    direct = M5LeasedWriteBroker(_SB(), kill)  # type: ignore[arg-type]
    with pytest.raises(ScopedAuthorityDenied, match="broker_contract_mismatch"):
        build_live_scoped_authority_broker(direct, _gateway(tmp_path, kill))


def test_live_factory_rejects_different_kill_switch_instance(tmp_path: Path) -> None:
    broker = build_live_m5_write_broker(_SB(), _kill(tmp_path))
    with pytest.raises(ScopedAuthorityDenied, match="broker_contract_mismatch"):
        build_live_scoped_authority_broker(broker, _gateway(tmp_path, _kill(tmp_path)))


def test_live_factory_accepts_factory_built_leased_broker(tmp_path: Path) -> None:
    kill = _kill(tmp_path)
    broker = build_live_m5_write_broker(_SB(), kill)
    scoped = build_live_scoped_authority_broker(broker, _gateway(tmp_path, kill))
    assert isinstance(scoped, ScopedAuthorityBroker)
    assert M5LeasedWriteBroker.scoped_authority_version >= 3


def test_slotted_sdk_facade_needs_no_wireagent_attributes(tmp_path: Path) -> None:
    sb = _SlottedSB()
    with pytest.raises(AttributeError):
        object.__setattr__(sb, "_wireagent_m5_write_state", object())

    kill = _kill(tmp_path)
    broker = build_live_m5_write_broker(sb, kill)
    assert isinstance(broker, M5LeasedWriteBroker)
    assert build_live_scoped_authority_broker(broker, _gateway(tmp_path, kill))


def test_same_sdk_facade_reuses_one_browser_write_lease(tmp_path: Path) -> None:
    sb = _SlottedSB()
    kill = _kill(tmp_path)
    first = build_live_m5_write_broker(sb, kill)
    second = build_live_m5_write_broker(sb, kill)

    assert first._sb is second._sb
    assert first._m5_write_state is second._m5_write_state
