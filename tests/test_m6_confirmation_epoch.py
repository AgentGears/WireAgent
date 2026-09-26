"""M6 Layer-3 confirmation epoch, clock, and synchronization regressions."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety import (
    DEFAULT_REGISTRY,
    ConfirmationState,
    DedupeStore,
    KillSwitch,
    RiskTier,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


class _Clock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _Cap:
    name = "like"

    def __init__(self, *, on_preview=None) -> None:
        self.execute_calls = 0
        self.preview_calls = 0
        self._on_preview = on_preview

    def compose(self, input: dict[str, Any], actor_identity: str | None) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.require("like")
        return WriteIntent(
            action_type="like",
            target_type="post",
            target_id=str(input.get("post_id", "1")),
            risk_meta=meta,
            compensation=comp,
            actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        del intent, broker
        self.preview_calls += 1
        if self._on_preview is not None:
            self._on_preview(self.preview_calls)
        return PreviewResult(summary="preview")

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        self.execute_calls += 1
        return ok_result(data={"executed": True})

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        return ok_result(data={"verified": True})


def _state(
    *,
    mono: _Clock | None = None,
    wall: _Clock | None = None,
    ttl: float = 300.0,
) -> ConfirmationState:
    return ConfirmationState(
        ttl_seconds=ttl,
        monotonic_clock=mono or _Clock(10.0),
        wall_clock=wall or _Clock(1_000.0),
        token_factory=lambda: "token-1",
    )


def _kernel(tmp_path: Path, state: ConfirmationState) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        KillSwitch(cfg),
        DEFAULT_REGISTRY,
        TokenBucket(),
        DedupeStore(ttl_seconds=3600),
        Journal(cfg),
        confirmation_state=state,
    )


def test_issue_captures_epoch_and_separates_wall_diagnostics_from_authority() -> None:
    mono = _Clock(50.0)
    wall = _Clock(5_000.0)
    state = _state(mono=mono, wall=wall, ttl=30.0)

    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )

    assert token.confirmation_epoch == 0
    assert token.created_at == 5_000.0
    assert token.expires_at == 5_030.0
    assert token.authority_created_at == 50.0
    assert token.authority_expires_at == 80.0


@pytest.mark.parametrize("wall_after", [-1_000_000.0, 9_000_000_000.0])
def test_wall_clock_jumps_do_not_change_token_validity(wall_after: float) -> None:
    mono = _Clock(10.0)
    wall = _Clock(1_000.0)
    state = _state(mono=mono, wall=wall, ttl=30.0)
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )

    wall.value = wall_after
    mono.value = 20.0
    consumed, blocked = state.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )

    assert blocked is None
    assert consumed is token
    assert token.consumed is True


def test_epoch_advance_revokes_all_older_tokens_including_unrelated() -> None:
    counter = 0

    def token_factory() -> str:
        nonlocal counter
        counter += 1
        return f"token-{counter}"

    state = ConfirmationState(
        ttl_seconds=300.0,
        monotonic_clock=_Clock(10.0),
        wall_clock=_Clock(1_000.0),
        token_factory=token_factory,
    )
    first = state.issue(
        intent_hash="intent-a",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    unrelated = state.issue(
        intent_hash="intent-b",
        risk_tier=RiskTier.PUBLIC_CONTENT_IRREVERSIBLE,
        capability_name="post_text",
    )

    assert state.advance_epoch() == 1

    for token, intent_hash, risk_tier, capability_name in (
        (first, "intent-a", RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT, "like"),
        (unrelated, "intent-b", RiskTier.PUBLIC_CONTENT_IRREVERSIBLE, "post_text"),
    ):
        _, blocked = state.validate_and_consume(
            token.token,
            intent_hash=intent_hash,
            risk_tier=risk_tier,
            capability_name=capability_name,
        )
        assert blocked == "stale_confirmation_epoch"
        assert token.consumed is False


def test_atomic_consume_allows_exactly_one_concurrent_winner() -> None:
    state = _state()
    token = state.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    barrier = threading.Barrier(3)
    results: list[str | None] = []
    results_lock = threading.Lock()

    def consume() -> None:
        barrier.wait()
        _, blocked = state.validate_and_consume(
            token.token,
            intent_hash="intent",
            risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
            capability_name="like",
        )
        with results_lock:
            results.append(blocked)

    threads = [threading.Thread(target=consume), threading.Thread(target=consume)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert sorted(str(item) for item in results) == ["None", "consumed_token"]


def test_consume_vs_epoch_advance_has_one_synchronized_order() -> None:
    for index in range(20):
        state = ConfirmationState(
            ttl_seconds=300.0,
            monotonic_clock=_Clock(10.0),
            wall_clock=_Clock(1_000.0),
            token_factory=lambda i=index: f"token-{i}",
        )
        token = state.issue(
            intent_hash="intent",
            risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
            capability_name="like",
        )
        barrier = threading.Barrier(3)
        outcome: list[str | None] = []

        def consume() -> None:
            barrier.wait()
            _, blocked = state.validate_and_consume(
                token.token,
                intent_hash="intent",
                risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
                capability_name="like",
            )
            outcome.append(blocked)

        def advance() -> None:
            barrier.wait()
            state.advance_epoch()

        consume_thread = threading.Thread(target=consume)
        advance_thread = threading.Thread(target=advance)
        consume_thread.start()
        advance_thread.start()
        barrier.wait()
        consume_thread.join(timeout=2)
        advance_thread.join(timeout=2)
        assert not consume_thread.is_alive()
        assert not advance_thread.is_alive()

        assert outcome in ([None], ["stale_confirmation_epoch"])
        assert state.current_epoch == 1
        if outcome == [None]:
            assert token.consumed is True
        else:
            assert token.consumed is False


async def test_kernel_rejects_old_epoch_token_before_execute(tmp_path: Path) -> None:
    counter = 0

    def token_factory() -> str:
        nonlocal counter
        counter += 1
        return f"token-{counter}"

    state = ConfirmationState(
        monotonic_clock=_Clock(10.0),
        wall_clock=_Clock(1_000.0),
        token_factory=token_factory,
    )
    kernel = _kernel(tmp_path, state)
    cap = _Cap()

    first = await kernel.execute(cap, object(), {"post_id": "1"}, actor_identity="alice")  # type: ignore[arg-type]
    token = first.data["data"]["confirmation_token"]
    assert first.data["data"]["confirmation_epoch"] == 0

    state.advance_epoch()
    second = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "1", "confirmation_token": token},
        actor_identity="alice",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "stale_confirmation_epoch"
    assert cap.execute_calls == 0


async def test_expiry_is_sampled_after_second_phase_preview(tmp_path: Path) -> None:
    mono = _Clock(10.0)
    wall = _Clock(1_000.0)
    state = ConfirmationState(
        ttl_seconds=5.0,
        monotonic_clock=mono,
        wall_clock=wall,
        token_factory=lambda: "token-1",
    )

    def on_preview(call_number: int) -> None:
        if call_number == 2:
            # Simulate blocking/read-only work consuming the remaining authority
            # lifetime after the token was presented but before final consume.
            mono.value = 16.0

    kernel = _kernel(tmp_path, state)
    cap = _Cap(on_preview=on_preview)
    first = await kernel.execute(cap, object(), {"post_id": "1"}, actor_identity="alice")  # type: ignore[arg-type]
    token = first.data["data"]["confirmation_token"]

    second = await kernel.execute(
        cap,
        object(),  # type: ignore[arg-type]
        {"post_id": "1", "confirmation_token": token},
        actor_identity="alice",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "expired_token"
    assert cap.execute_calls == 0


def test_new_confirmation_state_has_no_old_process_authority() -> None:
    first = _state()
    token = first.issue(
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    restarted = _state()

    assert restarted.current_epoch == 0
    _, blocked = restarted.validate_and_consume(
        token.token,
        intent_hash="intent",
        risk_tier=RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like",
    )
    assert blocked == "consumed_token"
