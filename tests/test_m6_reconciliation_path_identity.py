"""M6 Layer-4 safety-domain path identity regressions."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.config import WebWireConfig
from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    ReconciliationCoordinator,
    ReconciliationLedger,
    RecoveryGuard,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch


def _gateway(tmp_path: Path, effect_path: Path) -> CommitGateway:
    return CommitGateway(
        ledger=EffectLedger(path=effect_path),
        kill_switch=KillSwitch(WebWireConfig(state_dir=tmp_path / "kill")),
        authorization_epoch=AuthorizationEpoch(),
    )


def _guard(tmp_path: Path, effect_path: Path) -> RecoveryGuard:
    effects = EffectLedger(path=effect_path)
    reconciliations = ReconciliationLedger(
        path=tmp_path / "guard" / "reconciliations.ndjson"
    )
    return RecoveryGuard(effects, reconciliation_ledger=reconciliations)


def test_coordinator_rejects_different_effect_ledger_paths(tmp_path: Path) -> None:
    gateway = _gateway(tmp_path, tmp_path / "gateway" / "effects.ndjson")
    guard = _guard(tmp_path, tmp_path / "guard" / "effects.ndjson")

    with pytest.raises(ValueError, match="EffectLedger paths differ"):
        ReconciliationCoordinator(
            recovery_guard=guard,
            confirmation_state=ConfirmationState(),
            commit_gateway=gateway,
        )


def test_coordinator_path_check_uses_same_normcase_identity_as_registries(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    # POSIX normcase is intentionally a no-op, so simulate the case folding used
    # by a case-insensitive platform. The constructor must apply the same
    # normalization rule as the process-local shared-state registries.
    monkeypatch.setattr(os.path, "normcase", lambda value: value.casefold())

    gateway = _gateway(tmp_path, tmp_path / "CaseDomain" / "effects.ndjson")
    guard = _guard(tmp_path, tmp_path / "casedomain" / "effects.ndjson")
    confirmations = ConfirmationState()

    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
    )

    assert coordinator.commit_gateway is gateway
    assert coordinator.recovery_guard is guard
    assert coordinator.confirmation_state is confirmations
