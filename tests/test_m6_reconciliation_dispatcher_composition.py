"""M6 Layer-4 composition regressions at the Dispatcher authority root."""

from __future__ import annotations

from pathlib import Path

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.safety.effect_ledger import EffectLedgerRecord, EffectState
from webwire.safety.models import RiskTier
from webwire.safety.reconciliation_ledger import ReconciliationVerdict


def _evidence() -> dict[str, object]:
    return {
        "basis": "operator-review",
        "observed_at": "2026-09-26T20:20:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


async def _started_dispatcher(tmp_path: Path) -> Dispatcher:
    from types import SimpleNamespace

    from webwire.session import SessionManager

    class _StubSB:
        _page = None
        _controller = None

    class _StubSessionManager(SessionManager):
        def __init__(self, config) -> None:
            super().__init__(config)
            self._sb = _StubSB()  # type: ignore[assignment]
            self._started = True

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _StubSessionManager(cfg)
    dispatcher = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    dispatcher._install_m5_live_stack = (  # type: ignore[method-assign]
        lambda sb: setattr(dispatcher, "_m5_stack", SimpleNamespace(read_broker=object()))
    )
    started = await dispatcher.start()
    assert started.ok, getattr(started.error, "message", started)
    return dispatcher


async def test_dispatcher_owns_one_coherent_reconciliation_authority_domain(
    tmp_path: Path,
) -> None:
    dispatcher = await _started_dispatcher(tmp_path)
    session = dispatcher.create_reconciliation_operator_session("local-admin")

    assert session._coordinator is dispatcher._m6_reconciliation
    assert dispatcher._m6_reconciliation.confirmation_state is dispatcher._write_kernel.confirmation_state
    assert dispatcher._m6_reconciliation.commit_gateway is dispatcher._m5_gateway
    assert dispatcher._m6_reconciliation.recovery_guard is dispatcher._m5_recovery

    # Reconciliation remains local operator bookkeeping authority. It is not a
    # registered capability and therefore cannot acquire a hidden invoke() route.
    assert not any(name.startswith("recovery") or "reconcil" in name for name in dispatcher.capabilities)


async def test_dispatcher_reconciliation_revokes_real_writekernel_confirmation(
    tmp_path: Path,
) -> None:
    dispatcher = await _started_dispatcher(tmp_path)
    raw = EffectLedgerRecord(
        effect_id="fx-dispatcher-shared-epoch",
        semantic_key="actor|like|post|shared-epoch|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-reconcile",
        policy_binding="policy-reconcile",
        actor_id="actor",
        target_type="post",
        target_id="shared-epoch",
        timestamp="2026-09-26T20:19:00+00:00",
    )
    dispatcher._m5_ledger.append_durable(raw)
    dispatcher._m5_recovery.hydrate()

    confirmation_state = dispatcher._write_kernel.confirmation_state
    pending = confirmation_state.issue(
        intent_hash="unrelated-pending-intent",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark_post",
    )

    session = dispatcher.create_reconciliation_operator_session("local-admin")
    proposal = session.prepare_resolution(
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=_evidence(),
        evidence_summary="Operator verified the terminal effect evidence.",
    )
    authority = session.confirm_resolution(
        proposal.proposal_id,
        confirmation_text=proposal.confirmation_text,
    )
    resolution = session.resolve(proposal.proposal_id, authority=authority)

    assert resolution.confirmation_epoch == 1
    assert dispatcher._m5_recovery.require_clear(raw.semantic_key, refresh=False) is None

    token, reason = confirmation_state.validate_and_consume(
        pending.token,
        intent_hash="unrelated-pending-intent",
        risk_tier=RiskTier.PRIVATE_REVERSIBLE,
        capability_name="bookmark_post",
    )
    assert token is pending
    assert reason == "stale_confirmation_epoch"
