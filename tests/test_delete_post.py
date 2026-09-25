"""Capability-shell tests retained after ``delete_post`` moved to M5.

Remote mutation lifecycle coverage now lives in ``test_m5_delete_executor``,
``test_m5_delete_evidence``, ``test_m5_delete_adapter``, and Dispatcher routing
tests. This module pins the remaining legacy-shell responsibilities: canonical
intent composition, read-only preview behavior, and confirmation-token binding.
"""

from __future__ import annotations

from pathlib import Path

from webwire.capabilities.delete_post import DeletePostCapability
from webwire.config import WebWireConfig
from webwire.envelope import ok_result
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.execution_models import AttemptState
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.m5_delete_adapter import M5DeleteCapabilityAdapter
from webwire.safety.m5_delete_executor import M5DeleteExecution
from webwire.safety.risk_registry import DEFAULT_REGISTRY
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.write_kernel import WriteKernel

TARGET = {"post_url": "https://x.com/infaag/status/2102520857155522777"}


class _ReadBroker:
    async def navigate(self, url, **_):  # type: ignore[no-untyped-def]
        return ok_result(data={"url": url})

    async def probe_selectors(self, specs):  # type: ignore[no-untyped-def]
        return ok_result(
            data={"probes": {name: True for name in specs}, "count": len(specs)}
        )


class _NeverExecuteDelete:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self, intent):  # type: ignore[no-untyped-def]
        self.calls += 1
        return M5DeleteExecution(
            result=ok_result(data={"target_post_id": intent.target_id}),
            verification=ok_result(data={"post_state": "deleted"}),
            attempt_state=AttemptState.EFFECT_CONFIRMED,
            permit_issued=True,
            permit_consumed=True,
        )


def _kernel(tmp_path: Path) -> WriteKernel:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    return WriteKernel(
        kill_switch=KillSwitch(cfg),
        risk_registry=DEFAULT_REGISTRY,
        token_bucket=TokenBucket(),
        dedupe=DedupeStore(ttl_seconds=3600),
        journal=Journal(cfg),
        write_broker_factory=lambda: object(),
    )


def test_delete_compose_binds_target_from_status_url() -> None:
    cap = DeletePostCapability()

    intent = cap.compose(TARGET, actor_identity="@actor")

    assert intent.action_type == "delete_post"
    assert intent.target_id == "2102520857155522777"
    assert intent.payload["target_post_id"] == "2102520857155522777"
    assert intent.payload["post_url"] == TARGET["post_url"]
    assert intent.actor_identity == "@actor"


async def test_delete_confirmation_token_cannot_move_to_another_target(
    tmp_path: Path,
) -> None:
    cap = DeletePostCapability()
    executor = _NeverExecuteDelete()
    adapter = M5DeleteCapabilityAdapter(cap, executor)  # type: ignore[arg-type]
    kernel = _kernel(tmp_path)
    broker = _ReadBroker()

    first = await kernel.execute(
        adapter,
        broker,  # type: ignore[arg-type]
        TARGET,
        actor_identity="@actor",
    )
    assert first.data["policy"]["verdict"] == "confirmation_required"
    token = first.data["data"]["confirmation_token"]

    second = await kernel.execute(
        adapter,
        broker,  # type: ignore[arg-type]
        {
            "post_url": "https://x.com/infaag/status/999999",
            "confirmation_token": token,
        },
        actor_identity="@actor",
    )

    assert second.ok is False
    assert second.data["policy"]["blocked_by"] == "intent_mismatch"
    assert executor.calls == 0


async def test_delete_preview_warns_when_target_absent() -> None:
    class _ProbeBroker:
        async def navigate(self, url, **_):  # type: ignore[no-untyped-def]
            return ok_result(data={"url": url})

        async def probe_selectors(self, specs):  # type: ignore[no-untyped-def]
            return ok_result(
                data={
                    "probes": {name: False for name in specs},
                    "count": len(specs),
                }
            )

    cap = DeletePostCapability()
    intent = cap.compose(TARGET, actor_identity="@actor")

    preview = await cap.preview(intent, _ProbeBroker())

    assert preview.current_state == "post state: absent"
    assert any("TARGET NOT FOUND" in warning for warning in preview.warnings)
    assert any("IRREVERSIBLE" in warning for warning in preview.warnings)
