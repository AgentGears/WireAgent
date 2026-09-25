"""Transitional WriteKernel adapter for M5-migrated ``delete_post``."""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_delete_executor import M5DeleteExecution, M5DeleteExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5DeleteCapabilityAdapter"]


class M5DeleteCapabilityAdapter:
    """Keep legacy confirmation shell while hiding the mutation broker."""

    def __init__(self, capability: Any, executor: M5DeleteExecutor) -> None:
        name = getattr(capability, "name", "")
        if name != "delete_post":
            raise ValueError(f"unsupported M5 delete capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5DeleteExecution]] = ContextVar(
            f"m5_delete_execution_{id(self)}",
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
        del broker
        execution = await self._executor.execute(intent)
        if execution.attempt_state is AttemptState.EFFECT_UNKNOWN and execution.result.ok:
            # Defensive boundary: the current delete executor already returns a
            # hard UNKNOWN failure, but the transitional WriteKernel decides its
            # final verdict from execute_ok. Never let future executor drift turn
            # an unknown external outcome into an apparent ALLOW.
            self._execution.set(None)
            result = hard_failure(
                "M5 delete outcome is unknown; reconciliation required",
                failure_category=FailureCategory.UNKNOWN,
            )
            data = (
                dict(execution.result.data)
                if isinstance(execution.result.data, dict)
                else {}
            )
            data.update(
                {
                    "public_side_effect": True,
                    "reconciliation_required": True,
                    "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
                }
            )
            result.data = data
            return result
        if not execution.result.ok:
            self._execution.set(None)
            return execution.result
        self._execution.set(execution)
        return execution.result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 delete verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is AttemptState.EFFECT_CONFIRMED:
            if execution.verification is not None:
                return execution.verification
            return ok_result(data={"m5_effect_state": AttemptState.EFFECT_CONFIRMED.value})
        if execution.attempt_state is AttemptState.EFFECT_UNKNOWN:
            result = hard_failure(
                "M5 delete outcome is unknown; reconciliation required",
                failure_category=FailureCategory.UNKNOWN,
            )
            result.data = {
                "public_side_effect": True,
                "reconciliation_required": True,
                "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            }
            return result
        return soft_failure(
            "M5 delete execution did not reach EFFECT_CONFIRMED",
            failure_category=FailureCategory.UNKNOWN,
        )
