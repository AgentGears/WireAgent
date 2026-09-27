"""Fresh-process worker for M6 Layer-5 restart qualification.

This module is deliberately executed by subprocess tests rather than imported as
pytest tests.  M6 uses process-local class registries for lifecycle, publication,
ledger-ambiguity, and confirmation authority.  A new object in the pytest
process is therefore not a process restart.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Match tests/conftest.py when CI installs WireAgent with --no-deps.  A fresh
# Python process does not execute pytest conftest hooks before importing webwire.
try:
    import super_browser  # noqa: F401
except ImportError:  # pragma: no cover - CI-only subprocess path
    sys.path.insert(0, str(Path(__file__).parent / "stubs"))

from webwire.config import WebWireConfig
from webwire.safety import (
    ConfirmationState,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationCoordinator,
    ReconciliationLedger,
    ReconciliationOperatorSession,
    ReconciliationPersistenceError,
    ReconciliationVerdict,
    RecoveryGuard,
    RecoveryGuardUnavailable,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.reconciliation_ledger import ReconciliationLedgerError

_EFFECT_ID = "fx-layer5-restart"
_SEMANTIC_KEY = "actor|like|post|layer5-restart|"


def _cfg(state_dir: Path) -> WebWireConfig:
    return WebWireConfig(state_dir=state_dir, kill_env_var=None)


def _raw(state: EffectState) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=_EFFECT_ID,
        semantic_key=_SEMANTIC_KEY,
        state=state,
        action_type="like",
        intent_hash="intent-layer5-restart",
        policy_binding="policy-layer5-restart",
        actor_id="actor",
        target_type="post",
        target_id="layer5-restart",
        timestamp="2026-09-27T16:00:00+00:00",
    )


def _evidence() -> dict[str, object]:
    return {
        "basis": "layer5-restart-qualification",
        "observed_at": "2026-09-27T16:01:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def _stack(
    state_dir: Path,
    *,
    reconciliation_ledger: ReconciliationLedger | None = None,
    guard_type: type[RecoveryGuard] = RecoveryGuard,
) -> tuple[
    WebWireConfig,
    EffectLedger,
    ReconciliationLedger,
    RecoveryGuard,
    ConfirmationState,
    ReconciliationCoordinator,
]:
    cfg = _cfg(state_dir)
    effects = EffectLedger(cfg)
    reconciliations = reconciliation_ledger or ReconciliationLedger(
        path=cfg.reconciliations_path()
    )
    guard = guard_type(effects, reconciliation_ledger=reconciliations)
    confirmations = ConfirmationState()
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    coordinator = ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
        reconciliation_id_factory=lambda: "rec-layer5-restart",
        timestamp_factory=lambda: "2026-09-27T16:02:00+00:00",
    )
    return cfg, effects, reconciliations, guard, confirmations, coordinator


def _authority(
    coordinator: ReconciliationCoordinator,
    verdict: ReconciliationVerdict,
):
    evidence = _evidence()
    return coordinator._mint_operator_authority(
        effect_id=_EFFECT_ID,
        verdict=verdict,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: "auth-layer5-restart",
    )


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def create_reserved(state_dir: Path) -> None:
    effects = EffectLedger(_cfg(state_dir))
    effects.append_durable(_raw(EffectState.RESERVED))
    _emit({"created": "RESERVED"})


def create_reconciled(state_dir: Path, verdict_name: str) -> None:
    verdict = ReconciliationVerdict(verdict_name)
    _, effects, reconciliations, guard, confirmations, coordinator = _stack(state_dir)
    effects.append_durable(_raw(EffectState.EFFECT_UNKNOWN))
    authority = _authority(coordinator, verdict)
    resolution = coordinator.resolve(_EFFECT_ID, verdict, _evidence(), authority)
    _emit(
        {
            "resolved": True,
            "confirmation_epoch": confirmations.current_epoch,
            "reconciliations": len(reconciliations.read_authoritative()),
            "guard_available": guard.status().available,
            "verdict": resolution.record.verdict.value,
        }
    )


def crash_after_durable_before_publish(state_dir: Path, verdict_name: str) -> None:
    verdict = ReconciliationVerdict(verdict_name)

    class CrashOnRefreshGuard(RecoveryGuard):
        def refresh(self):  # type: ignore[no-untyped-def]
            os._exit(73)

    _, effects, _, _, _, coordinator = _stack(
        state_dir,
        guard_type=CrashOnRefreshGuard,
    )
    effects.append_durable(_raw(EffectState.EFFECT_UNKNOWN))
    authority = _authority(coordinator, verdict)
    coordinator.resolve(_EFFECT_ID, verdict, _evidence(), authority)
    raise AssertionError("coordinator returned instead of crashing at publication")


def probe_guard(state_dir: Path) -> None:
    cfg = _cfg(state_dir)
    guard = RecoveryGuard(EffectLedger(cfg))
    status = guard.hydrate()
    block = guard.require_clear(_SEMANTIC_KEY, refresh=False)
    effective_states = (
        []
        if block is None
        else [item.effective_state.value for item in block.effects]
    )
    new_confirmations = ConfirmationState()
    _emit(
        {
            "available": status.available,
            "blocked": block is not None,
            "effective_states": effective_states,
            "pending_confirmation_count": len(
                new_confirmations._diagnostic_pending_tokens()
            ),
            "reconciliation_count": len(
                guard.reconciliation_ledger.read_authoritative()
            ),
        }
    )


def probe_startup_fsync(state_dir: Path) -> None:
    cfg = _cfg(state_dir)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    called = False
    redurable = reconciliations._redurable_existing_file

    def tracking_redurable() -> None:
        nonlocal called
        called = True
        redurable()

    reconciliations._redurable_existing_file = tracking_redurable  # type: ignore[method-assign]
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    status = guard.hydrate()
    _emit(
        {
            "available": status.available,
            "startup_redurability_called": called,
            "blocked": guard.require_clear(_SEMANTIC_KEY, refresh=False) is not None,
        }
    )


def probe_startup_fsync_failure(state_dir: Path) -> None:
    cfg = _cfg(state_dir)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())

    def fail_redurable() -> None:
        raise ReconciliationLedgerError("injected fresh-process startup fsync failure")

    reconciliations._redurable_existing_file = fail_redurable  # type: ignore[method-assign]
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    try:
        guard.hydrate()
    except RecoveryGuardUnavailable:
        status = guard.status()
        _emit(
            {
                "available": status.available,
                "hydrated": status.hydrated,
                "error": status.error or "",
            }
        )
        return
    raise AssertionError("startup re-durability failure did not fail closed")


def clean_failure_no_row(state_dir: Path) -> None:
    cfg = _cfg(state_dir)

    class CleanFailLedger(ReconciliationLedger):
        def append_durable(self, record):  # type: ignore[no-untyped-def]
            del record
            raise ReconciliationLedgerError("injected clean failure before bytes")

    reconciliations = CleanFailLedger(path=cfg.reconciliations_path())
    _, effects, _, guard, confirmations, coordinator = _stack(
        state_dir,
        reconciliation_ledger=reconciliations,
    )
    effects.append_durable(_raw(EffectState.EFFECT_UNKNOWN))
    guard.hydrate()
    authority = _authority(coordinator, ReconciliationVerdict.CONFIRMED_EFFECT)
    try:
        coordinator.resolve(
            _EFFECT_ID,
            ReconciliationVerdict.CONFIRMED_EFFECT,
            _evidence(),
            authority,
        )
    except ReconciliationPersistenceError as exc:
        _emit(
            {
                "ambiguous": exc.ambiguous,
                "authority_committed": authority.committed,
                "confirmation_epoch": confirmations.current_epoch,
                "row_exists": cfg.reconciliations_path().exists()
                and cfg.reconciliations_path().stat().st_size > 0,
            }
        )
        return
    raise AssertionError("clean persistence failure was not surfaced")


def resolve_fresh_after_restart(state_dir: Path) -> None:
    _, _, reconciliations, guard, _, coordinator = _stack(state_dir)
    guard.hydrate()
    session = ReconciliationOperatorSession(
        coordinator=coordinator,
        operator_id="local-admin",
        proposal_id_factory=lambda: "proposal-after-restart",
        monotonic_clock=lambda: 20.0,
    )
    proposal = session.prepare_resolution(
        effect_id=_EFFECT_ID,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=_evidence(),
        evidence_summary="Fresh confirmation after process restart.",
    )
    authority = session.confirm_resolution(
        proposal.proposal_id,
        confirmation_text=proposal.confirmation_text,
    )
    resolution = session.resolve(proposal.proposal_id, authority=authority)
    _emit(
        {
            "fresh_confirmation_used": True,
            "resolved": True,
            "verdict": resolution.record.verdict.value,
            "reconciliation_count": len(reconciliations.read_authoritative()),
        }
    )


def main() -> None:
    if len(sys.argv) < 3:
        raise SystemExit("usage: _m6_restart_worker.py MODE STATE_DIR [ARG]")
    mode = sys.argv[1]
    state_dir = Path(sys.argv[2])
    state_dir.mkdir(parents=True, exist_ok=True)
    arg = sys.argv[3] if len(sys.argv) > 3 else ""

    if mode == "create_reserved":
        create_reserved(state_dir)
    elif mode == "create_reconciled":
        create_reconciled(state_dir, arg)
    elif mode == "crash_after_durable_before_publish":
        crash_after_durable_before_publish(state_dir, arg)
    elif mode == "probe_guard":
        probe_guard(state_dir)
    elif mode == "probe_startup_fsync":
        probe_startup_fsync(state_dir)
    elif mode == "probe_startup_fsync_failure":
        probe_startup_fsync_failure(state_dir)
    elif mode == "clean_failure_no_row":
        clean_failure_no_row(state_dir)
    elif mode == "resolve_fresh_after_restart":
        resolve_fresh_after_restart(state_dir)
    else:
        raise SystemExit(f"unknown mode: {mode}")


if __name__ == "__main__":
    main()
