"""Trip-generation safety regressions for authorization revocation."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch


def test_resetting_earlier_listener_cannot_suppress_gateway_epoch_bump(
    tmp_path: Path,
) -> None:
    """Every listener registered for a trip generation must observe that event.

    The reset listener is deliberately registered *before* the CommitGateway's
    epoch listener. It clears the live kill state during generation-1 delivery,
    but generation 1 remains owed to the gateway listener, so revocation cannot
    disappear merely because the switch is reset before notification completes.
    """
    cfg = WebWireConfig(state_dir=tmp_path)
    kill = KillSwitch(cfg)
    reset_calls = 0

    def reset_first() -> None:
        nonlocal reset_calls
        reset_calls += 1
        kill.reset()

    kill.add_trip_listener(reset_first)
    epoch = AuthorizationEpoch()
    CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=kill,
        authorization_epoch=epoch,
    )

    kill.trip()

    assert reset_calls == 1
    assert kill.tripped() is False
    assert epoch.current == 1
