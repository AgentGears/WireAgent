"""Transitional WriteKernel adapter for the M5-migrated ``quote_post`` capability.

The legacy WriteKernel still owns compose/preview/confirmation during staged
Layer-5 migration. This adapter preserves that shell while ensuring the original
``QuotePostCapability`` never receives the legacy mutation broker after
confirmation. Execution and verification are bound to one M5 quote receipt.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_quote_executor import M5QuoteExecution, M5QuoteExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5QuoteCapabilityAdapter"]


class M5QuoteCapabilityAdapter:
    """Hide the legacy mutation broker from ``QuotePostCapability``."""

    def __init__(self, capability: Any, executor: M5QuoteExecutor) -> None:
        name = getattr(capability, "name", "")
        if name != "quote_post":
            raise ValueError(f"unsupported M5 quote capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5QuoteExecution]] = ContextVar(
            f"m5_quote_execution_{id(self)}",
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
        """Return explicit quote evidence already bound to the M5 receipt."""
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 quote verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is AttemptState.EFFECT_CONFIRMED:
            if execution.verification is not None:
                return execution.verification
            return ok_result(
                data={"m5_effect_state": AttemptState.EFFECT_CONFIRMED.value}
            )
        return soft_failure(
            "M5 quote execution did not reach EFFECT_CONFIRMED",
            failure_category=FailureCategory.UNKNOWN,
        )
