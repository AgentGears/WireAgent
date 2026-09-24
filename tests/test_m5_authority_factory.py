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
from webwire.safety.m5_authority_factory import build_live_scoped_authority_broker
from webwire.safety.scoped_authority import ScopedAuthorityBroker, ScopedAuthorityDenied


class _OldBroker:
    scoped_authority_version = 2


class _CurrentBrokerMarker:
    scoped_authority_version = M5LeasedWriteBroker.scoped_authority_version


def _gateway(tmp_path: Path) -> CommitGateway:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
    )


def test_live_factory_rejects_provenance_only_broker(tmp_path: Path) -> None:
    with pytest.raises(ScopedAuthorityDenied, match="broker_contract_mismatch"):
        build_live_scoped_authority_broker(_OldBroker(), _gateway(tmp_path))


def test_live_factory_accepts_current_leased_contract(tmp_path: Path) -> None:
    scoped = build_live_scoped_authority_broker(
        _CurrentBrokerMarker(),
        _gateway(tmp_path),
    )
    assert isinstance(scoped, ScopedAuthorityBroker)
    assert M5LeasedWriteBroker.scoped_authority_version >= 3
