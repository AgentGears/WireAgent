"""Layer-5 execution adapter for the first migrated engagement effects.

This trusted adapter deliberately sits above capability code and below the
WriteKernel migration. It turns one human-confirmed M5ExecutionSession into an
exact scoped effect invocation, then terminalizes the receipt from independent
evidence.

Initial scope is intentionally narrow: bookmark and like. Both authorities
expose only ``apply()``. Content preparation/submission and delete have distinct
lifecycles and are migrated separately.

Outcome rule:
- no permit issued before the canonical mutation seam -> proven NO_EFFECT when
  the attempt is still clean precommit;
- permit issued but not consumed -> explicit gateway-owned NO_EFFECT closure;
- permit consumed -> verify the canonical target through a read-only evidence
  surface; record EFFECT_CONFIRMED only when the mutation call succeeded and
  readback proves the intended state, otherwise record EFFECT_UNKNOWN.

The caller-owned WriteIntent is never re-read after ``runtime.issue``. Once a
permit exists, verification reconstructs the status URL from the permit's
immutable target binding.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from webwire.envelope import ActionResult
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime
from webwire.safety.models import WriteIntent

__all__ = [
    "M5EffectExecution",
    "M5EffectExecutor",
    "M5EffectExecutorDenied",
    "M5StateEvidenceReader",
]

_SUPPORTED_ACTIONS = frozenset({"bookmark", "like"})
_EXPECTED_STATE = {
    "bookmark": ("bookmark_state", "bookmarked"),
    "like": ("like_state", "liked"),
}


class M5EffectExecutorDenied(RuntimeError):
    """The engagement executor was asked to run outside its migrated scope."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"M5 effect executor denied: {reason}"
            + (f" — {detail}" if detail else "")
        )


@runtime_checkable
class M5StateEvidenceReader(Protocol):
    """Read-only evidence surface retained outside capability mutation authority."""

    async def read_bookmark_state(self, post_url: str) -> ActionResult: ...

    async def read_like_state(self, post_url: str) -> ActionResult: ...


@dataclass(frozen=True)
class M5EffectExecution:
    """Trusted orchestration result for one exact M5 effect attempt."""

    result: ActionResult
    verification: Optional[ActionResult]
    attempt_state: AttemptState
    permit_issued: bool
    permit_consumed: bool


class M5EffectExecutor:
    """Execute and terminalize the first Layer-5 engagement canaries."""

    def __init__(
        self,
        *,
        runtime: M5ExecutionRuntime,
        evidence_reader: M5StateEvidenceReader,
    ) -> None:
        self._runtime = runtime
        self._evidence = evidence_reader

    async def execute(self, intent: WriteIntent) -> M5EffectExecution:
        session = self._runtime.issue(intent)
        action = session.action_type
        if action not in _SUPPORTED_ACTIONS:
            session.resolve_no_external_effect(reason="effect_executor_unsupported_action")
            raise M5EffectExecutorDenied("unsupported_action", action)

        receipt = session.scope_effect()
        apply = getattr(receipt.authority, "apply", None)
        if not callable(apply):
            session.resolve_no_external_effect(reason="effect_authority_shape_mismatch")
            raise M5EffectExecutorDenied("authority_shape_mismatch", action)

        result = await apply()
        permit = receipt.permit

        if permit is None:
            # authorize_commit can fail after REQUIRED reservation I/O began.
            # That state is intentionally unresolved and cannot be converted to
            # generic clean-precommit NO_EFFECT merely because no permit exists.
            if not session.attempt.reservation_started:
                session.resolve_no_external_effect(
                    reason=(
                        "effect_already_satisfied"
                        if result.ok
                        else "effect_failed_before_commit_authority"
                    )
                )
            return M5EffectExecution(
                result=result,
                verification=None,
                attempt_state=session.attempt.state,
                permit_issued=False,
                permit_consumed=False,
            )

        if not permit.consumed:
            session.resolve_no_external_effect(reason="permit_issued_but_not_consumed")
            return M5EffectExecution(
                result=result,
                verification=None,
                attempt_state=session.attempt.state,
                permit_issued=True,
                permit_consumed=False,
            )

        verification = await self._verify(
            action=action,
            target_id=permit.target_id,
        )
        state_key, expected_state = _EXPECTED_STATE[action]
        verified_state = (
            (verification.data or {}).get(state_key)
            if verification.ok and isinstance(verification.data, dict)
            else None
        )

        evidence = {
            "mutation_ok": bool(result.ok),
            "verification_ok": bool(verification.ok),
            "verified_state": verified_state,
            "expected_state": expected_state,
        }
        if result.ok and verification.ok and verified_state == expected_state:
            session.record_confirmed(evidence=evidence)
        else:
            failure_category = getattr(verification, "failure_category", None)
            if failure_category is not None:
                evidence["verification_failure_category"] = str(failure_category)
            session.record_unknown(evidence=evidence)

        return M5EffectExecution(
            result=result,
            verification=verification,
            attempt_state=session.attempt.state,
            permit_issued=True,
            permit_consumed=True,
        )

    async def _verify(self, *, action: str, target_id: str) -> ActionResult:
        post_url = f"https://x.com/i/status/{target_id}"
        if action == "bookmark":
            return await self._evidence.read_bookmark_state(post_url)
        if action == "like":
            return await self._evidence.read_like_state(post_url)
        raise M5EffectExecutorDenied("unsupported_action", action)
