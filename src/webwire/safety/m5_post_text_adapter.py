"""Transitional WriteKernel adapter for the M5-migrated ``post_text`` capability.

The legacy WriteKernel still drives compose/preview/confirmation and passes a
write broker into ``execute``/``verify``. This adapter preserves that protocol
shape while ensuring the original PostTextCapability never receives the legacy
mutation surface. Post-confirmation execution is delegated exclusively to
``M5PostTextExecutor``.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_post_text_executor import M5PostTextExecution, M5PostTextExecutor
from webwire.safety.models import WriteIntent

__all__ = ["M5PostTextCapabilityAdapter"]


class M5PostTextCapabilityAdapter:
    """Hide the legacy mutation broker from ``PostTextCapability``."""

    def __init__(self, capability: Any, executor: M5PostTextExecutor) -> None:
        name = getattr(capability, "name", "")
        if name != "post_text":
            raise ValueError(f"unsupported M5 post-text capability: {name!r}")
        self._capability = capability
        self._executor = executor
        self._execution: ContextVar[Optional[M5PostTextExecution]] = ContextVar(
            f"m5_post_text_execution_{id(self)}",
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
            # WriteKernel skips verify() after failed execution. Clear any prior
            # context so a later invocation cannot observe stale evidence.
            self._execution.set(None)
            return execution.result
        self._execution.set(execution)
        return execution.result

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Return evidence already bound to the exact M5 execution receipt."""
        del intent, broker
        execution = self._execution.get()
        self._execution.set(None)
        if execution is None:
            return soft_failure(
                "M5 post-text verification has no execution receipt",
                failure_category=FailureCategory.SECURITY,
            )
        if execution.attempt_state is AttemptState.EFFECT_CONFIRMED:
            if execution.verification is not None:
                return execution.verification
            return ok_result(
                data={"m5_effect_state": AttemptState.EFFECT_CONFIRMED.value}
            )
        return soft_failure(
            "M5 post-text execution did not reach EFFECT_CONFIRMED",
            failure_category=FailureCategory.UNKNOWN,
        )
