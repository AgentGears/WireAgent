"""M6 Layer-5 fault/concurrency qualification missing from Layers 1-4."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    ConfirmationState,
    DedupeStore,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    ReconciliationCoordinator,
    ReconciliationDenied,
    ReconciliationLedger,
    ReconciliationRecord,
    ReconciliationVerdict,
    RecoveryGuard,
    TokenBucket,
    WriteIntent,
    WriteKernel,
    canonical_evidence_hash,
)
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.execution_models import AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.write_kernel import PreviewResult


def _evidence(label: str = "qualified") -> dict[str, object]:
    return {
        "basis": "layer5-qualification",
        "observed_at": "2026-09-27T16:10:00+00:00",
        "observations": [{"kind": "operator", "value": label}],
    }


class _LikeCapability:
    name = "like"

    def __init__(self) -> None:
        self.preview_calls = 0
        self.execute_calls = 0

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        meta, compensation = DEFAULT_REGISTRY.require("like")
        return WriteIntent(
            action_type="like",
            target_type="post",
            target_id=str(input.get("post_id", "123")),
            risk_meta=meta,
            compensation=compensation,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del broker
        self.preview_calls += 1
        return PreviewResult(summary=f"preview {intent.target_id}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        self.execute_calls += 1
        return ok_result(data={"liked": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _effect_for_intent(intent: WriteIntent, *, effect_id: str) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=intent.dedupe_key(),
        state=EffectState.EFFECT_UNKNOWN,
        action_type=intent.action_type,
        intent_hash=intent.intent_hash(),
        policy_binding="policy-layer5-race",
        actor_id=intent.actor_identity,
        target_type=intent.target_type,
        target_id=intent.target_id,
        timestamp="2026-09-27T16:11:00+00:00",
    )


def _coordinator(
    cfg: WebWireConfig,
    effects: EffectLedger,
    reconciliations: ReconciliationLedger,
    guard: RecoveryGuard,
    confirmations: ConfirmationState,
) -> ReconciliationCoordinator:
    gateway = CommitGateway(
        ledger=effects,
        kill_switch=KillSwitch(cfg),
        authorization_epoch=AuthorizationEpoch(),
    )
    return ReconciliationCoordinator(
        recovery_guard=guard,
        confirmation_state=confirmations,
        commit_gateway=gateway,
    )


def _authority(
    coordinator: ReconciliationCoordinator,
    *,
    effect_id: str,
    verdict: ReconciliationVerdict,
    evidence: dict[str, object],
    authority_id: str,
):
    return coordinator._mint_operator_authority(
        effect_id=effect_id,
        verdict=verdict,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="local-admin",
        ttl_seconds=120.0,
        monotonic_clock=lambda: 10.0,
        authority_id_factory=lambda: authority_id,
    )


def _kernel(
    cfg: WebWireConfig,
    guard: RecoveryGuard,
    confirmations: ConfirmationState,
) -> WriteKernel:
    return WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        recovery_guard=guard,
        confirmation_state=confirmations,
    )


class _BlockingAppendLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.append_entered = threading.Event()
        self.append_release = threading.Event()

    def append_durable(self, record: ReconciliationRecord) -> None:
        self.append_entered.set()
        if not self.append_release.wait(timeout=5):
            raise AssertionError("timed out waiting to release reconciliation append")
        super().append_durable(record)


class _BlockingNextReadLedger(ReconciliationLedger):
    def __init__(self, *, path: Path) -> None:
        super().__init__(path=path)
        self.read_entered = threading.Event()
        self.read_release = threading.Event()
        self._block_next = False

    def block_next_read(self) -> None:
        self._block_next = True
        self.read_entered.clear()
        self.read_release.clear()

    def read_authoritative(self) -> list[ReconciliationRecord]:
        if self._block_next:
            self._block_next = False
            self.read_entered.set()
            if not self.read_release.wait(timeout=5):
                raise AssertionError("timed out waiting to release reconciliation read")
        return super().read_authoritative()


def test_r39_reconciliation_wins_then_old_token_is_stale_after_clear(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects = EffectLedger(cfg)
    reconciliations = _BlockingAppendLedger(path=cfg.reconciliations_path())
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    confirmations = ConfirmationState()
    coordinator = _coordinator(cfg, effects, reconciliations, guard, confirmations)
    capability = _LikeCapability()
    intent = capability.compose({"post_id": "123"}, "actor")
    effect = _effect_for_intent(intent, effect_id="fx-r39-reconcile-first")
    effects.append_durable(effect)
    guard.hydrate()
    token = confirmations.issue(
        intent_hash=intent.intent_hash(),
        risk_tier=intent.risk_tier(),
        capability_name=capability.name,
    )
    evidence = _evidence("reconciliation-wins")
    authority = _authority(
        coordinator,
        effect_id=effect.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        evidence=evidence,
        authority_id="auth-r39-reconcile-first",
    )
    kernel = _kernel(cfg, guard, confirmations)

    reconcile_errors: list[BaseException] = []
    write_results: list[ActionResult] = []
    write_done = threading.Event()

    def run_reconcile() -> None:
        try:
            coordinator.resolve(
                effect.effect_id,
                ReconciliationVerdict.CONFIRMED_NO_EFFECT,
                evidence,
                authority,
            )
        except BaseException as exc:  # noqa: BLE001 - test thread must surface failure
            reconcile_errors.append(exc)

    def run_write() -> None:
        try:
            result = asyncio.run(
                kernel.execute(
                    capability,
                    object(),  # type: ignore[arg-type]
                    {"post_id": "123", "confirmation_token": token.token},
                    actor_identity="actor",
                )
            )
            write_results.append(result)
        finally:
            write_done.set()

    reconcile_thread = threading.Thread(target=run_reconcile)
    reconcile_thread.start()
    assert reconciliations.append_entered.wait(timeout=5)
    assert confirmations.current_epoch == 1

    write_thread = threading.Thread(target=run_write)
    write_thread.start()
    assert not write_done.wait(timeout=0.05)

    reconciliations.append_release.set()
    reconcile_thread.join(timeout=5)
    write_thread.join(timeout=5)
    assert not reconcile_thread.is_alive()
    assert not write_thread.is_alive()
    assert reconcile_errors == []
    assert len(write_results) == 1
    assert write_results[0].data["policy"]["blocked_by"] == "stale_confirmation_epoch"
    assert capability.execute_calls == 0
    assert guard.require_clear(effect.semantic_key, refresh=False) is None


def test_r39_write_refresh_wins_and_observes_old_blocked_truth(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects = EffectLedger(cfg)
    reconciliations = _BlockingNextReadLedger(path=cfg.reconciliations_path())
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    confirmations = ConfirmationState()
    coordinator = _coordinator(cfg, effects, reconciliations, guard, confirmations)
    capability = _LikeCapability()
    intent = capability.compose({"post_id": "456"}, "actor")
    effect = _effect_for_intent(intent, effect_id="fx-r39-write-first")
    effects.append_durable(effect)
    guard.hydrate()
    token = confirmations.issue(
        intent_hash=intent.intent_hash(),
        risk_tier=intent.risk_tier(),
        capability_name=capability.name,
    )
    evidence = _evidence("write-refresh-wins")
    authority = _authority(
        coordinator,
        effect_id=effect.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence,
        authority_id="auth-r39-write-first",
    )
    kernel = _kernel(cfg, guard, confirmations)
    reconciliations.block_next_read()

    write_results: list[ActionResult] = []
    reconcile_errors: list[BaseException] = []

    def run_write() -> None:
        write_results.append(
            asyncio.run(
                kernel.execute(
                    capability,
                    object(),  # type: ignore[arg-type]
                    {"post_id": "456", "confirmation_token": token.token},
                    actor_identity="actor",
                )
            )
        )

    def run_reconcile() -> None:
        try:
            coordinator.resolve(
                effect.effect_id,
                ReconciliationVerdict.CONFIRMED_EFFECT,
                evidence,
                authority,
            )
        except BaseException as exc:  # noqa: BLE001 - test thread must surface failure
            reconcile_errors.append(exc)

    write_thread = threading.Thread(target=run_write)
    write_thread.start()
    assert reconciliations.read_entered.wait(timeout=5)

    reconcile_thread = threading.Thread(target=run_reconcile)
    reconcile_thread.start()
    assert confirmations.current_epoch == 0

    reconciliations.read_release.set()
    write_thread.join(timeout=5)
    reconcile_thread.join(timeout=5)
    assert not write_thread.is_alive()
    assert not reconcile_thread.is_alive()
    assert reconcile_errors == []
    assert len(write_results) == 1
    assert write_results[0].data["policy"]["blocked_by"] == "reconciliation_required"
    assert capability.preview_calls == 0
    assert capability.execute_calls == 0
    assert confirmations.current_epoch == 1
    _, stale_reason = confirmations.validate_and_consume(
        token.token,
        intent_hash=intent.intent_hash(),
        risk_tier=intent.risk_tier(),
        capability_name=capability.name,
    )
    assert stale_reason == "stale_confirmation_epoch"
    assert guard.require_clear(effect.semantic_key, refresh=False) is None


def test_r29_competing_coordinator_verdicts_have_one_terminal_winner(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    confirmations = ConfirmationState()
    coordinator = _coordinator(cfg, effects, reconciliations, guard, confirmations)
    raw = EffectLedgerRecord(
        effect_id="fx-r29",
        semantic_key="actor|like|post|r29|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-r29",
        policy_binding="policy-r29",
        actor_id="actor",
        target_type="post",
        target_id="r29",
        timestamp="2026-09-27T16:12:00+00:00",
    )
    effects.append_durable(raw)
    guard.hydrate()

    evidence_effect = _evidence("effect")
    evidence_no_effect = _evidence("no-effect")
    authority_effect = _authority(
        coordinator,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence=evidence_effect,
        authority_id="auth-r29-effect",
    )
    authority_no_effect = _authority(
        coordinator,
        effect_id=raw.effect_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        evidence=evidence_no_effect,
        authority_id="auth-r29-no-effect",
    )
    barrier = threading.Barrier(3)
    outcomes: list[str] = []
    outcome_lock = threading.Lock()

    def resolve(
        verdict: ReconciliationVerdict,
        evidence: dict[str, object],
        authority: Any,
    ) -> None:
        barrier.wait()
        try:
            coordinator.resolve(raw.effect_id, verdict, evidence, authority)
            outcome = "committed"
        except ReconciliationDenied as exc:
            outcome = exc.reason
        with outcome_lock:
            outcomes.append(outcome)

    threads = [
        threading.Thread(
            target=resolve,
            args=(
                ReconciliationVerdict.CONFIRMED_EFFECT,
                evidence_effect,
                authority_effect,
            ),
        ),
        threading.Thread(
            target=resolve,
            args=(
                ReconciliationVerdict.CONFIRMED_NO_EFFECT,
                evidence_no_effect,
                authority_no_effect,
            ),
        ),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert sorted(outcomes) == ["already_reconciled", "committed"]
    records = reconciliations.read_authoritative()
    assert len(records) == 1
    assert confirmations.current_epoch == 1


def test_r25_forged_invocation_journal_data_has_no_recovery_authority(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects = EffectLedger(cfg)
    raw = EffectLedgerRecord(
        effect_id="fx-r25",
        semantic_key="actor|post|post|r25|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="post",
        intent_hash="intent-r25",
        policy_binding="policy-r25",
        actor_id="actor",
        target_type="post",
        target_id="r25",
        timestamp="2026-09-27T16:13:00+00:00",
    )
    effects.append_durable(raw)
    forged = {
        "effect_id": raw.effect_id,
        "semantic_key": raw.semantic_key,
        "verdict": "CONFIRMED_NO_EFFECT",
        "reconciliation_id": "forged-journal-reconciliation",
    }
    cfg.journal_path().write_text(json.dumps(forged) + "\n", encoding="utf-8")

    guard = RecoveryGuard(effects)
    guard.hydrate()

    block = guard.require_clear(raw.semantic_key, refresh=False)
    assert block is not None
    assert block.effect_ids == (raw.effect_id,)
    assert guard.reconciliation_ledger.read_authoritative() == []


def test_r44_old_reconciliation_fact_has_no_ttl_or_audit_style_rotation(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    effects = EffectLedger(cfg)
    reconciliations = ReconciliationLedger(path=cfg.reconciliations_path())
    raw = EffectLedgerRecord(
        effect_id="fx-r44",
        semantic_key="actor|like|post|r44|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-r44",
        policy_binding="policy-r44",
        actor_id="actor",
        target_type="post",
        target_id="r44",
        timestamp="2000-01-01T00:00:00+00:00",
    )
    effects.append_durable(raw)
    evidence = {
        "basis": "historical-operator-resolution",
        "observed_at": "2000-01-01T00:01:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }
    reconciliation = ReconciliationRecord(
        reconciliation_id="rec-r44",
        effect_id=raw.effect_id,
        semantic_key=raw.semantic_key,
        action_type=raw.action_type,
        intent_hash=raw.intent_hash,
        policy_binding=raw.policy_binding,
        actor_id=raw.actor_id,
        target_type=raw.target_type,
        target_id=raw.target_id,
        verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
        operator_id="local-admin",
        evidence_hash=canonical_evidence_hash(evidence),
        evidence=evidence,
        timestamp="2000-01-01T00:02:00+00:00",
    )
    reconciliations.append_durable(reconciliation)

    later_ledger = ReconciliationLedger(path=cfg.reconciliations_path())
    assert later_ledger.read_authoritative() == [reconciliation]
    guard = RecoveryGuard(effects, reconciliation_ledger=later_ledger)
    guard.hydrate()
    assert guard.require_clear(raw.semantic_key, refresh=False) is None
    assert later_ledger.path.read_text(encoding="utf-8").count("\n") == 1
