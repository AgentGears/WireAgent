"""Transitional WriteKernel adapter for the M5-migrated ``reply_post`` capability.

The legacy WriteKernel still owns compose/preview/confirmation during staged
Layer-5 migration. This adapter preserves that protocol while ensuring the
original ``ReplyPostCapability`` never receives the legacy mutation broker after
confirmation. Execution is delegated exclusively to ``M5ReplyExecutor`` and
verification returns evidence bound to that exact M5 execution receipt.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_reply_executor import M5ReplyExecution, M5ReplyExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5ReplyCapabilityAdapter"]


class M5ReplyCapabilityAdapter:
    """Hide the legacy mutation broker from ``ReplyPostCapability``."""

    def __init__(self, capability: Any, executor: M5ReplyExecutor) -> None:
        name = getattr(capability, "name", "")
        if name != "reply_post":
            raise ValueError(f"unsupported M5 reply capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5ReplyExecution]] = ContextVar(
            f"m5_reply_execution_{id(self)}",
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
        """Execute through M5; the legacy kernel broker is intentionally ignored."""
        del broker
        execution = await self._executor.execute(intent)
        if not execution.result.ok:
            self._execution.set(None)
            return execution.result
        self._execution.set(execution)
        return execution.result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Return thread evidence already bound to the exact M5 receipt."""
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 reply verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is AttemptState.EFFECT_CONFIRMED:
            if execution.verification is not None:
                return execution.verification
            return ok_result(
                data={"m5_effect_state": AttemptState.EFFECT_CONFIRMED.value}
            )
        return soft_failure(
            "M5 reply execution did not reach EFFECT_CONFIRMED",
            failure_category=FailureCategory.UNKNOWN,
        )
