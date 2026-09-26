"""M6 Layer-2 integration: recovery reconciliation clears no independent gate."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    BucketLimits,
    DedupeStore,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    KillSwitch,
    ReconciliationLedger,
    ReconciliationRecord,
    ReconciliationVerdict,
    RecoveryGuard,
    TokenBucket,
    WriteIntent,
    WriteKernel,
    canonical_evidence_hash,
)
from webwire.safety.write_kernel import PreviewResult


class _GuardedLikeCapability:
    name = "like"

    def __init__(self) -> None:
        self.preview_calls = 0

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.require("like")
        return WriteIntent(
            action_type="like",
            target_type="post",
            target_id=str(input.get("post_id", "123")),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del broker
        self.preview_calls += 1
        return PreviewResult(summary=f"preview {intent.target_id}")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"liked": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _reconciled_guard(tmp_path: Path) -> tuple[RecoveryGuard, str]:
    effects = EffectLedger(path=tmp_path / "effects.ndjson")
    reconciliations = ReconciliationLedger(path=tmp_path / "reconciliations.ndjson")
    key = "alice|like|post|123|"
    effect = EffectLedgerRecord(
        effect_id="fx-reconciled",
        semantic_key=key,
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="alice",
        target_type="post",
        target_id="123",
        timestamp="2026-09-26T06:10:00+00:00",
    )
    effects.append_durable(effect)
    evidence = {
        "basis": "operator-resolution",
        "observed_at": "2026-09-26T06:11:00+00:00",
        "observations": [{"kind": "operator", "value": "reviewed"}],
    }
    reconciliations.append_durable(
        ReconciliationRecord(
            reconciliation_id="rec-reconciled",
            effect_id=effect.effect_id,
            semantic_key=effect.semantic_key,
            action_type=effect.action_type,
            intent_hash=effect.intent_hash,
            policy_binding=effect.policy_binding,
            actor_id=effect.actor_id,
            target_type=effect.target_type,
            target_id=effect.target_id,
            verdict=ReconciliationVerdict.CONFIRMED_NO_EFFECT,
            operator_id="operator-local",
            evidence_hash=canonical_evidence_hash(evidence),
            evidence=evidence,
            timestamp="2026-09-26T06:12:00+00:00",
        )
    )
    guard = RecoveryGuard(effects, reconciliation_ledger=reconciliations)
    guard.hydrate()
    assert guard.require_clear(key, refresh=False) is None
    return guard, key


def _kernel(
    tmp_path: Path,
    *,
    guard: RecoveryGuard,
    kill: KillSwitch,
    bucket: TokenBucket,
    dedupe: DedupeStore,
) -> WriteKernel:
    return WriteKernel(
        kill,
        DEFAULT_REGISTRY,
        bucket,
        dedupe,
        Journal(WebWireConfig(state_dir=tmp_path, kill_env_var=None)),
        recovery_guard=guard,
    )


async def test_recovery_clear_does_not_bypass_live_dedupe(tmp_path: Path) -> None:
    guard, key = _reconciled_guard(tmp_path)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    dedupe = DedupeStore(ttl_seconds=3600)
    dedupe.record(key)
    kernel = _kernel(
        tmp_path,
        guard=guard,
        kill=kill,
        bucket=TokenBucket(),
        dedupe=dedupe,
    )
    cap = _GuardedLikeCapability()

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "dedupe"
    assert cap.preview_calls == 0


async def test_recovery_clear_does_not_refund_exhausted_token_bucket(
    tmp_path: Path,
) -> None:
    cap = _GuardedLikeCapability()
    intent = cap.compose({"post_id": "123"}, "alice")
    bucket = TokenBucket(
        limits={
            "like": BucketLimits(max_count=1, window_seconds=300),
            "_global": BucketLimits(max_count=20, window_seconds=300),
        }
    )
    allowed, _ = bucket.acquire("like", intent.risk_tier())
    assert allowed is True

    guard, _ = _reconciled_guard(tmp_path)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kernel = _kernel(
        tmp_path,
        guard=guard,
        kill=KillSwitch(cfg),
        bucket=bucket,
        dedupe=DedupeStore(ttl_seconds=3600),
    )

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "token_bucket"
    assert cap.preview_calls == 0


async def test_recovery_clear_does_not_reset_or_bypass_kill(tmp_path: Path) -> None:
    guard, _ = _reconciled_guard(tmp_path)
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    kill.trip()
    assert kill.tripped() is True

    kernel = _kernel(
        tmp_path,
        guard=guard,
        kill=kill,
        bucket=TokenBucket(),
        dedupe=DedupeStore(ttl_seconds=3600),
    )
    cap = _GuardedLikeCapability()

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "kill_switch"
    assert kill.tripped() is True
    assert cap.preview_calls == 0


class _StartProbeSession:
    def __init__(self) -> None:
        self.start_calls = 0
        self.sb = None
        self.session_loaded_state = "none"
        self.ownership = "owned"
        self.resolved_handle = None

    async def start(self) -> ActionResult:
        self.start_calls += 1
        return ok_result()


async def test_dispatcher_corrupt_reconciliation_fails_before_browser_start(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    cfg.reconciliations_path().parent.mkdir(parents=True, exist_ok=True)
    cfg.reconciliations_path().write_text("{broken\n", encoding="utf-8")
    session = _StartProbeSession()
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]

    result = await dispatcher.start()

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert session.start_calls == 0
    assert dispatcher._m5_recovery.status().available is False
