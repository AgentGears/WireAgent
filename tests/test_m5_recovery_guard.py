"""Layer-6 RecoveryGuard hydration and pre-browser replay-denial regressions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from webwire.capabilities.compose_post import ComposePostCapability
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    KillSwitch,
    RecoveryGuard,
    RecoveryGuardUnavailable,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


def _record(
    semantic_key: str,
    state: EffectState,
    *,
    effect_id: str = "effect-1",
    action_type: str = "like",
    target_id: str = "123",
) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id=effect_id,
        semantic_key=semantic_key,
        state=state,
        action_type=action_type,
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="alice",
        target_type="post",
        target_id=target_id,
    )


def test_empty_ledger_hydrates_clear(tmp_path: Path) -> None:
    guard = RecoveryGuard(EffectLedger(path=tmp_path / "effects.ndjson"))

    status = guard.hydrate()

    assert status.hydrated is True
    assert status.available is True
    assert status.unresolved_semantic_keys == ()
    assert status.unresolved_effect_count == 0
    assert guard.require_clear("alice|like|post|123|", refresh=False) is None


def test_reserved_and_unknown_block_exact_semantic_key(tmp_path: Path) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    key = "alice|like|post|123|"
    ledger.append_durable(_record(key, EffectState.RESERVED, effect_id="effect-a"))
    ledger.append_durable(
        _record(
            key,
            EffectState.EFFECT_UNKNOWN,
            effect_id="effect-b",
        )
    )
    guard = RecoveryGuard(ledger)
    guard.hydrate()

    block = guard.require_clear(key, refresh=False)

    assert block is not None
    assert block.semantic_key == key
    assert block.effect_ids == ("effect-a", "effect-b")
    assert [effect.raw_state for effect in block.effects] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    assert all(
        effect.effective_state is EffectState.EFFECT_UNKNOWN
        for effect in block.effects
    )
    assert guard.require_clear("alice|like|post|999|", refresh=False) is None


def test_no_effect_and_confirmed_do_not_block(tmp_path: Path) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    no_effect_key = "alice|post|post|none|a"
    confirmed_key = "alice|post|post|none|b"

    ledger.append_durable(
        _record(
            no_effect_key,
            EffectState.RESERVED,
            effect_id="effect-no-effect",
            action_type="post",
            target_id="none",
        )
    )
    ledger.append_durable(
        _record(
            no_effect_key,
            EffectState.NO_EFFECT,
            effect_id="effect-no-effect",
            action_type="post",
            target_id="none",
        )
    )
    ledger.append_durable(
        _record(
            confirmed_key,
            EffectState.EFFECT_CONFIRMED,
            effect_id="effect-confirmed",
            action_type="post",
            target_id="none",
        )
    )

    guard = RecoveryGuard(ledger)
    status = guard.hydrate()

    assert status.unresolved_semantic_keys == ()
    assert guard.require_clear(no_effect_key, refresh=False) is None
    assert guard.require_clear(confirmed_key, refresh=False) is None


def test_refresh_catches_unknown_appended_after_startup(tmp_path: Path) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    guard = RecoveryGuard(ledger)
    key = "alice|like|post|123|"
    guard.hydrate()
    assert guard.require_clear(key, refresh=False) is None

    ledger.append_durable(_record(key, EffectState.EFFECT_UNKNOWN))

    block = guard.require_clear(key, refresh=True)
    assert block is not None
    assert block.effect_ids == ("effect-1",)


def test_corrupt_ledger_makes_guard_unavailable(tmp_path: Path) -> None:
    path = tmp_path / "effects.ndjson"
    path.write_text("{not-json\n", encoding="utf-8")
    guard = RecoveryGuard(EffectLedger(path=path))

    with pytest.raises(RecoveryGuardUnavailable):
        guard.hydrate()

    status = guard.status()
    assert status.hydrated is True
    assert status.available is False
    assert status.unresolved_semantic_keys == ()
    assert status.error is not None
    with pytest.raises(RecoveryGuardUnavailable):
        guard.require_clear("alice|like|post|123|", refresh=False)


class _GuardedLikeCapability:
    name = "like"

    def __init__(self) -> None:
        self.preview_calls = 0
        self.execute_calls = 0

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
        self.execute_calls += 1
        return ok_result(data={"liked": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _kernel_with_guard(
    tmp_path: Path,
    guard: RecoveryGuard,
) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        recovery_guard=guard,
    )


async def test_kernel_denies_unresolved_before_preview_or_confirmation(
    tmp_path: Path,
) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    key = "alice|like|post|123|"
    ledger.append_durable(_record(key, EffectState.EFFECT_UNKNOWN))
    guard = RecoveryGuard(ledger)
    guard.hydrate()
    kernel = _kernel_with_guard(tmp_path, guard)
    cap = _GuardedLikeCapability()

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "reconciliation_required"
    assert result.data["data"]["reconciliation_required"] is True
    assert result.data["data"]["semantic_key"] == key
    assert cap.preview_calls == 0
    assert cap.execute_calls == 0
    assert kernel._pending_tokens == {}


async def test_kernel_refresh_blocks_new_same_process_unknown(tmp_path: Path) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    guard = RecoveryGuard(ledger)
    guard.hydrate()
    kernel = _kernel_with_guard(tmp_path, guard)
    cap = _GuardedLikeCapability()

    first = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )
    assert first.data["policy"]["verdict"] == "confirmation_required"
    assert cap.preview_calls == 1

    key = "alice|like|post|123|"
    ledger.append_durable(_record(key, EffectState.EFFECT_UNKNOWN))

    second = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )
    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "reconciliation_required"
    assert cap.preview_calls == 1


async def test_kernel_recovery_unavailable_fails_closed_before_preview(
    tmp_path: Path,
) -> None:
    path = tmp_path / "effects.ndjson"
    guard = RecoveryGuard(EffectLedger(path=path))
    guard.hydrate()
    kernel = _kernel_with_guard(tmp_path, guard)
    cap = _GuardedLikeCapability()
    path.write_text("{corrupt\n", encoding="utf-8")

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "reconciliation_required"
    assert result.data["data"]["recovery_unavailable"] is True
    assert cap.preview_calls == 0


async def test_caller_flag_cannot_bypass_recovery_guard(tmp_path: Path) -> None:
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    key = "alice|like|post|123|"
    ledger.append_durable(_record(key, EffectState.EFFECT_UNKNOWN))
    guard = RecoveryGuard(ledger)
    guard.hydrate()
    kernel = _kernel_with_guard(tmp_path, guard)
    cap = _GuardedLikeCapability()

    result = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "123"},
        actor_identity="alice",
        enforce_recovery_guard=False,
    )

    assert result.ok is False
    assert result.data["policy"]["blocked_by"] == "reconciliation_required"
    assert result.data["trace"]["recovery_exemption_ignored"] is True
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


async def test_dispatcher_corrupt_recovery_fails_before_session_start(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    cfg.effects_path().parent.mkdir(parents=True, exist_ok=True)
    cfg.effects_path().write_text("{broken\n", encoding="utf-8")
    session = _StartProbeSession()
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]

    result = await dispatcher.start()

    assert result.ok is False
    assert result.failure_category.value == "security"
    assert session.start_calls == 0


async def test_dispatcher_compose_post_bypasses_matching_recovery_block(
    tmp_path: Path,
) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    session = _StartProbeSession()
    session.resolved_handle = "alice"
    dispatcher = Dispatcher(cfg, session_manager=session)  # type: ignore[arg-type]
    dispatcher._broker = object()  # type: ignore[assignment]

    compose = ComposePostCapability()
    intent = compose.compose({"text": "hello"}, "alice")
    dispatcher._m5_ledger.append_durable(
        _record(
            intent.dedupe_key(),
            EffectState.EFFECT_UNKNOWN,
            action_type="post",
            target_id="none",
        )
    )
    dispatcher._m5_recovery.hydrate()

    result = await dispatcher.invoke("compose_post", {"text": "hello"})

    assert result.ok is True
    assert result.data["policy"]["verdict"] == "confirmation_required"
