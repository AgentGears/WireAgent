"""Trusted WriteKernel adapter for the first Layer-5 capability migration.

The legacy WriteKernel protocol passes a broker object to ``execute`` and
``verify``. During staged migration we preserve that protocol shape without
letting the original bookmark/like capability receive the legacy mutation
surface: this adapter delegates only compose/preview to the capability and owns
all post-confirmation execution through ``M5EffectExecutor``.

This is an explicit migration shim, not the final kernel shape. Once all write
capabilities use scoped authority directly, Layer 5 can remove the legacy
execute/verify broker parameters from the supported path.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_effect_executor import M5EffectExecution, M5EffectExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5EngagementCapabilityAdapter"]

_MIGRATED_CAPABILITY_NAMES = frozenset({"bookmark_post", "like_post"})


class M5EngagementCapabilityAdapter:
    """Hide the legacy mutation broker from one migrated engagement capability."""

    def __init__(self, capability: Any, executor: M5EffectExecutor) -> None:
        name = getattr(capability, "name", "")
        if name not in _MIGRATED_CAPABILITY_NAMES:
            raise ValueError(f"unsupported M5 engagement capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5EffectExecution]] = ContextVar(
            f"m5_execution_{name}_{id(self)}",
            default=None,
        )
        self.name = name

    @property
    def tier(self) -> Any:
        return self._capability.tier

    def compose(
        self,
        input: dict[str, Any],
        actor_identity: Optional[str],
    ) -> WriteIntent:
        return self._capability.compose(input, actor_identity)

    async def preview(self, intent: WriteIntent, broker: Any) -> Any:
        return await self._capability.preview(intent, broker)

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Execute through M5; ``broker`` is intentionally ignored.

        The legacy kernel decides its final policy verdict from ``execute_ok``.
        Therefore an M5 ``EFFECT_UNKNOWN`` must be translated to an explicit
        non-retryable failure even when the underlying click call returned
        ``ok=True``. ``public_side_effect`` is retained so the transitional
        journal-backed dedupe layer also blocks an in-process clean replay.
        """
        del broker
        execution = await self._executor.execute(intent)
        result = execution.result

        if execution.attempt_state is AttemptState.EFFECT_UNKNOWN:
            self._execution.set(None)
            data = dict(result.data) if isinstance(result.data, dict) else {}
            data.update(
                {
                    "public_side_effect": True,
                    "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
                    "reconciliation_required": True,
                }
            )
            denied = hard_failure(
                "M5 effect outcome is unknown; reconciliation is required",
                failure_category=FailureCategory.UNKNOWN,
            )
            denied.data = data
            return denied

        if not result.ok:
            # WriteKernel skips verify() after a failed execute; do not leave a
            # stale ContextVar receipt that a later invocation could observe.
            self._execution.set(None)
            return result

        self._execution.set(execution)
        return result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Report evidence already acquired by the M5 executor.

        The legacy broker parameter is ignored; verification truth comes from
        the exact M5 execution receipt, not from a second unrestricted broker
        handoff to capability code.
        """
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is AttemptState.EFFECT_CONFIRMED:
            if execution.verification is not None:
                return execution.verification
            return ok_result(data={"m5_effect_state": AttemptState.EFFECT_CONFIRMED.value})
        if execution.attempt_state is AttemptState.NO_EFFECT and execution.result.ok:
            return ok_result(
                data={
                    "m5_effect_state": AttemptState.NO_EFFECT.value,
                    "result": "already_satisfied",
                }
            )
        if execution.attempt_state is AttemptState.EFFECT_UNKNOWN:
            return hard_failure(
                "M5 effect outcome is unknown; reconciliation is required",
                failure_category=FailureCategory.UNKNOWN,
            )
        return soft_failure(
            f"M5 effect did not reach a verified terminal success: "
            f"{execution.attempt_state.value}",
            failure_category=FailureCategory.UNKNOWN,
        )
