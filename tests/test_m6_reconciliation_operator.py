"""M6 Layer-4 explicit local operator workflow regressions."""

from __future__ import annotations

from pathlib import Path

import pytest

from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationCoordinator,
    ReconciliationLedger,
    ReconciliationOperatorError,
    ReconciliationOperatorSession,
    ReconciliationVerdict,
    RecoveryGuard,
)


def _evidence() -> dict[str, object]:
    return {
        "basis": "local-operator-inspection",
        "observed_at": "2026-09-26T19:20:00+00:00",
        "observations": [{"kind": "state", "value": "present"}],
    }


def _session(tmp_path: Path) -> tuple[
    ReconciliationOperatorSession,
    ReconciliationLedger,
    RecoveryGuard,
]:
    effects = EffectLedger(path=tmp_path / "effects.ndjson")
    reconciliations = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    raw = EffectLedgerRecord(
        effect_id="fx-operator",
        semantic_key="remote-actor|like|post|123|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-operator",
        policy_binding="policy-operator",
        actor_id="remote-actor",
        target_type="post",
        target_id="123",
        timestamp="2026-09-26T19:19:00+00:00",
    )
    effects.append_durable(raw)
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    guard.hydrate()
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=ConfirmationState(),
        reconciliation_id_factory=lambda: "rec-operator",
        timestamp_factory=lambda: "2026-09-26T19:21:00+00:00",
    )
    session = ReconciliationOperatorSession(
        coordinator=coordinator,
        operator_id="local-admin",
        proposal_id_factory=lambda: "proposal-1",
        monotonic_clock=lambda: 10.0,
    )
    return session, reconciliations, guard


def test_prepare_is_read_only_and_does_not_mint_authority(tmp_path: Path) -> None:
    session, reconciliations, guard = _session(tmp_path)

    proposal = session.prepare_resolution(
        effect_id="fx-operator",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=_evidence(),
        evidence_summary="Observed the target state directly.",
    )

    assert proposal.effect_id == "fx-operator"
    assert proposal.actor_id == "remote-actor"
    assert proposal.operator_id == "local-admin"
    assert proposal.actor_id != proposal.operator_id
    assert proposal.confirmation_text.startswith(
        "CONFIRM fx-operator CONFIRMED_EFFECT "
    )
    assert reconciliations.read_authoritative() == []
    assert guard.require_clear("remote-actor|like|post|123|", refresh=False) is not None


def test_proposal_evidence_is_detached_from_caller_mutation(tmp_path: Path) -> None:
    session, _reconciliations, _guard = _session(tmp_path)
    evidence = _evidence()
    proposal = session.prepare_resolution(
        effect_id="fx-operator",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence,
        evidence_summary="Observed target state.",
    )

    evidence["basis"] = "mutated-after-display"
    detached = proposal.evidence
    assert detached["basis"] == "local-operator-inspection"
    detached["basis"] = "mutated-copy"
    assert proposal.evidence["basis"] == "local-operator-inspection"


def test_exact_same_session_confirmation_mints_one_authority(tmp_path: Path) -> None:
    session, _reconciliations, _guard = _session(tmp_path)
    proposal = session.prepare_resolution(
        effect_id="fx-operator",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=_evidence(),
        evidence_summary="Observed target state.",
    )

    with pytest.raises(ReconciliationOperatorError) as exc_info:
        session.confirm_resolution(
            proposal.proposal_id,
            confirmation_text="yes",
        )
    assert exc_info.value.reason == "confirmation_mismatch"

    authority = session.confirm_resolution(
        proposal.proposal_id,
        confirmation_text=proposal.confirmation_text,
    )
    repeated = session.confirm_resolution(
        proposal.proposal_id,
        confirmation_text=proposal.confirmation_text,
    )

    assert repeated is authority
    assert authority.effect_id == proposal.effect_id
    assert authority.verdict is proposal.verdict
    assert authority.evidence_hash == proposal.evidence_hash
    assert authority.operator_id == "local-admin"


def test_confirmed_proposal_resolves_exact_fact_and_is_single_use(tmp_path: Path) -> None:
    session, reconciliations, guard = _session(tmp_path)
    proposal = session.prepare_resolution(
        effect_id="fx-operator",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=_evidence(),
        evidence_summary="Observed target state.",
    )
    authority = session.confirm_resolution(
        proposal.proposal_id,
        confirmation_text=proposal.confirmation_text,
    )

    resolution = session.resolve(proposal.proposal_id, authority=authority)

    assert resolution.record.reconciliation_id == "rec-operator"
    assert resolution.record.operator_id == "local-admin"
    assert resolution.record.actor_id == "remote-actor"
    assert resolution.record.evidence_hash == proposal.evidence_hash
    assert authority.consumed is True
    assert len(reconciliations.read_authoritative()) == 1
    assert guard.require_clear("remote-actor|like|post|123|", refresh=False) is None

    with pytest.raises(ReconciliationOperatorError) as exc_info:
        session.resolve(proposal.proposal_id, authority=authority)
    assert exc_info.value.reason == "proposal_resolved"


def test_unconfirmed_proposal_cannot_resolve(tmp_path: Path) -> None:
    session, _reconciliations, _guard = _session(tmp_path)
    proposal = session.prepare_resolution(
        effect_id="fx-operator",
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        evidence=_evidence(),
        evidence_summary="Observed target absence with domain proof.",
    )

    # A separately manufactured object is insufficient: the session must have
    # minted the exact authority after explicit confirmation.
    from webwire.safety import ReconciliationAuthority

    rogue = ReconciliationAuthority(
        effect_id=proposal.effect_id,
        verdict=proposal.verdict,
        evidence_hash=proposal.evidence_hash,
        operator_id="local-admin",
        monotonic_clock=lambda: 10.0,
    )
    with pytest.raises(ReconciliationOperatorError) as exc_info:
        session.resolve(proposal.proposal_id, authority=rogue)
    assert exc_info.value.reason == "proposal_not_confirmed"
