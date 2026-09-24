"""Layer-5 engagement effect executor tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from super_browser.results.types import FailureCategory

from webwire.config import WebWireConfig
from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.commit_gateway import CommitGateway
from webwire.safety.effect_ledger import EffectLedger, EffectState
from webwire.safety.effect_policy import DEFAULT_EFFECT_POLICIES
from webwire.safety.execution_models import AttemptState, AuthorizationEpoch
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_effect_executor import M5EffectExecutor
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.models import WriteIntent
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.scoped_authority import ScopedAuthorityBroker


class _EngagementBroker:
    def __init__(self, *, mode: str = "success") -> None:
        self.mode = mode
        self.gate_calls = 0

    async def click_bookmark(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        return await self._apply("bookmark", post_url, _commit_gate)

    async def click_like(
        self,
        post_url: str,
        *,
        _commit_gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        return await self._apply("like", post_url, _commit_gate)

    async def _apply(
        self,
        action: str,
        post_url: str,
        gate,  # type: ignore[no-untyped-def]
    ) -> ActionResult:
        if self.mode == "already":
            return ok_result(data={"result": "already_satisfied", "action": action})
        if self.mode == "fail_before_gate":
            return soft_failure(
                "probe failed before commit",
                failure_category=FailureCategory.TIMEOUT,
            )
        self.gate_calls += 1
        denied = gate()
        if denied is not None:
            return denied
        if self.mode == "raise_after_gate":
            raise RuntimeError(f"{action} mutation interrupted")
        if self.mode == "fail_after_gate":
            return soft_failure(
                f"{action} click failed after authority",
                failure_category=FailureCategory.UNKNOWN,
            )
        return ok_result(data={"action": action, "post_url": post_url})


class _EvidenceReader:
    def __init__(
        self,
        *,
        bookmark_state: str = "bookmarked",
        like_state: str = "liked",
        raise_for: str | None = None,
    ) -> None:
        self.bookmark_state = bookmark_state
        self.like_state = like_state
        self.raise_for = raise_for
        self.urls: list[tuple[str, str]] = []

    async def read_bookmark_state(self, post_url: str) -> ActionResult:
        self.urls.append(("bookmark", post_url))
        if self.raise_for == "bookmark":
            raise RuntimeError("bookmark verification interrupted")
        return ok_result(data={"bookmark_state": self.bookmark_state})

    async def read_like_state(self, post_url: str) -> ActionResult:
        self.urls.append(("like", post_url))
        if self.raise_for == "like":
            raise RuntimeError("like verification interrupted")
        return ok_result(data={"like_state": self.like_state})


class _KillBeforeConsumeGateway(CommitGateway):
    def __init__(self, *, kill_switch: KillSwitch, **kwargs: Any) -> None:
        super().__init__(kill_switch=kill_switch, **kwargs)
        self._test_kill = kill_switch
        self._trip_before_consume = True

    def consume_permit(self, permit, **kwargs):  # type: ignore[no-untyped-def]
        if self._trip_before_consume:
            self._trip_before_consume = False
            self._test_kill.trip()
        return super().consume_permit(permit, **kwargs)


def _intent(action: str) -> WriteIntent:
    risk, compensation = DEFAULT_REGISTRY.require(action)
    return WriteIntent(
        action_type=action,
        target_type="post",
        target_id="123",
        risk_meta=risk,
        compensation=compensation,
        payload={"post_url": "https://x.com/approved/status/123"},
        actor_identity="@actor",
    )


def _executor(
    tmp_path: Path,
    *,
    broker: _EngagementBroker,
    reader: _EvidenceReader,
    kill_before_consume: bool = False,
) -> tuple[M5EffectExecutor, EffectLedger, KillSwitch]:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kill = KillSwitch(cfg)
    ledger = EffectLedger(cfg)
    gateway_cls = _KillBeforeConsumeGateway if kill_before_consume else CommitGateway
    gateway = gateway_cls(
        ledger=ledger,
        kill_switch=kill,
        authorization_epoch=AuthorizationEpoch(),
        policies=DEFAULT_EFFECT_POLICIES,
        permit_ttl_seconds=60.0,
    )
    scoped = ScopedAuthorityBroker(
        broker,
        gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    runtime = M5ExecutionRuntime(
        scoped_authority=scoped,
        commit_gateway=gateway,
        policies=DEFAULT_EFFECT_POLICIES,
    )
    return M5EffectExecutor(runtime=runtime, evidence_reader=reader), ledger, kill


async def test_bookmark_success_requires_independent_readback_for_confirmed(
    tmp_path: Path,
) -> None:
    reader = _EvidenceReader(bookmark_state="bookmarked")
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(),
        reader=reader,
    )

    execution = await executor.execute(_intent("bookmark"))

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert execution.permit_issued is True
    assert execution.permit_consumed is True
    assert reader.urls == [("bookmark", "https://x.com/i/status/123")]
    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.EFFECT_CONFIRMED]
    assert records[0].details["mutation_ok"] is True
    assert records[0].details["verified_state"] == "bookmarked"


async def test_like_success_records_reservation_then_confirmed(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(),
        reader=_EvidenceReader(like_state="liked"),
    )

    execution = await executor.execute(_intent("like"))

    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
    ]


async def test_inconclusive_like_verification_records_unknown(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(),
        reader=_EvidenceReader(like_state="unknown"),
    )

    execution = await executor.execute(_intent("like"))

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    assert records[-1].details["verified_state"] == "unknown"


async def test_failed_mutation_after_consumption_stays_unknown_even_if_state_matches(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(mode="fail_after_gate"),
        reader=_EvidenceReader(bookmark_state="bookmarked"),
    )

    execution = await executor.execute(_intent("bookmark"))

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.EFFECT_UNKNOWN]
    assert records[0].details["mutation_ok"] is False
    assert records[0].details["verified_state"] == "bookmarked"


async def test_already_satisfied_closes_clean_no_effect_without_ledger_fact(
    tmp_path: Path,
) -> None:
    broker = _EngagementBroker(mode="already")
    reader = _EvidenceReader()
    executor, ledger, _ = _executor(tmp_path, broker=broker, reader=reader)

    execution = await executor.execute(_intent("bookmark"))

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert broker.gate_calls == 0
    assert reader.urls == []
    assert ledger.read_records() == []


async def test_precommit_failure_closes_clean_no_effect_without_ledger_fact(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(mode="fail_before_gate"),
        reader=_EvidenceReader(),
    )

    execution = await executor.execute(_intent("bookmark"))

    assert execution.result.ok is False
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is False
    assert ledger.read_records() == []


async def test_kill_denied_like_consumption_closes_reserved_no_effect(
    tmp_path: Path,
) -> None:
    executor, ledger, kill = _executor(
        tmp_path,
        broker=_EngagementBroker(),
        reader=_EvidenceReader(),
        kill_before_consume=True,
    )

    execution = await executor.execute(_intent("like"))

    assert execution.result.ok is False
    assert kill.tripped() is True
    assert execution.attempt_state is AttemptState.NO_EFFECT
    assert execution.permit_issued is True
    assert execution.permit_consumed is False
    assert [record.state for record in ledger.read_records()] == [
        EffectState.RESERVED,
        EffectState.NO_EFFECT,
    ]


async def test_mutation_exception_after_consumption_records_unknown_before_reraise(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(mode="raise_after_gate"),
        reader=_EvidenceReader(),
    )

    with pytest.raises(RuntimeError, match="like mutation interrupted"):
        await executor.execute(_intent("like"))

    records = ledger.read_records()
    assert [record.state for record in records] == [
        EffectState.RESERVED,
        EffectState.EFFECT_UNKNOWN,
    ]
    assert records[-1].details["phase"] == "mutation"
    assert records[-1].details["exception_type"] == "RuntimeError"


async def test_verification_exception_records_unknown_before_reraise(
    tmp_path: Path,
) -> None:
    executor, ledger, _ = _executor(
        tmp_path,
        broker=_EngagementBroker(),
        reader=_EvidenceReader(raise_for="bookmark"),
    )

    with pytest.raises(RuntimeError, match="bookmark verification interrupted"):
        await executor.execute(_intent("bookmark"))

    records = ledger.read_records()
    assert [record.state for record in records] == [EffectState.EFFECT_UNKNOWN]
    assert records[0].details["phase"] == "verification"
    assert records[0].details["mutation_ok"] is True
    assert records[0].details["exception_type"] == "RuntimeError"
