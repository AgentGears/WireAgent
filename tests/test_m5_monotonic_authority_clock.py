"""Regression for process-local M5 authority clocks.

Authority validity is elapsed-time state and therefore uses monotonic clocks.
Durable ledger timestamps remain UTC wall-clock provenance.
"""

from __future__ import annotations

import time
from pathlib import Path

from webwire.config import WebWireConfig
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectLedgerRecord, EffectState
from webwire.safety.execution_models import (
    DEFAULT_GRANT_TTL_S,
    ApprovalGrantStore,
    AuthorizationEpoch,
)
from webwire.safety.kill_switch import KillSwitch


def test_default_grant_authority_clock_is_monotonic() -> None:
    before = time.monotonic()
    store = ApprovalGrantStore()
    grant = store.mint(
        intent_hash="intent",
        actor_id="@actor",
        action_type="post",
        target_type="post",
        target_id="123",
        policy_binding="policy",
        authorization_epoch=0,
    )
    after = time.monotonic()

    assert store._clock is time.monotonic
    assert grant.clock is time.monotonic
    assert before <= grant.issued_at <= after
    assert grant.expires_at >= before + DEFAULT_GRANT_TTL_S


def test_default_permit_authority_clock_is_monotonic(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path)
    gateway = CommitGateway(
        ledger=EffectLedger(cfg),
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )

    assert gateway._clock is time.monotonic


def test_effect_ledger_keeps_wall_clock_utc_timestamp() -> None:
    record = EffectLedgerRecord(
        effect_id="effect",
        semantic_key="semantic",
        state=EffectState.EFFECT_CONFIRMED,
        action_type="like",
        intent_hash="intent",
        policy_binding="policy",
    )

    assert "T" in record.timestamp
    assert record.timestamp.endswith("+00:00")
