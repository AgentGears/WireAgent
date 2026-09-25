"""Layer-5 scoped execution for target-bound deletion."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result, soft_failure
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime, M5ExecutionSession
from webwire.safety.models import WriteIntent
from webwire.safety.scoped_authority import AuthorizedEffect, DeletePostAuthority, ScopedAuthorityDenied

__all__ = ["M5DeleteEvidence", "M5DeleteExecution", "M5DeleteExecutor"]


@runtime_checkable
class M5DeleteEvidence(Protocol):
    async def read_delete_state(self, post_url: str, post_id: str) -> ActionResult: ...


@dataclass(frozen=True)
class M5DeleteExecution:
    result: ActionResult
    verification: Optional[ActionResult]
    attempt_state: AttemptState
    permit_issued: bool
    permit_consumed: bool


class M5DeleteExecutor:
    """Execute one approved delete and terminalize only from strict evidence."""

    def __init__(self, *, runtime: M5ExecutionRuntime, evidence_reader: M5DeleteEvidence) -> None:
        self._runtime = runtime
        self._evidence = evidence_reader

    async def execute(self, intent: WriteIntent) -> M5DeleteExecution:
        frozen = deepcopy(intent)
        session = self._runtime.issue(frozen)
        if session.action_type != "delete_post":
            session.resolve_no_external_effect(reason="delete_executor_wrong_action")
            return self._clean_failure(session, "delete executor requires delete_post")

        target_id = frozen.payload.get("target_post_id", "")
        post_url = frozen.payload.get("post_url", "")
        if not isinstance(target_id, str) or not target_id.isdigit():
            session.resolve_no_external_effect(reason="delete_target_invalid")
            return self._clean_failure(session, "approved delete target is invalid")
        if not isinstance(post_url, str):
            session.resolve_no_external_effect(reason="delete_url_invalid")
            return self._clean_failure(session, "approved delete URL is invalid")
        bound_url = post_url or f"https://x.com/i/status/{target_id}"

        try:
            baseline = await self._evidence.read_delete_state(bound_url, target_id)
        except BaseException as exc:
            session.resolve_no_external_effect(reason="delete_baseline_interrupted")
            result = hard_failure(
                f"delete baseline interrupted before authority: {type(exc).__name__}",
                failure_category=FailureCategory.UNKNOWN,
            )
            result.data = {"public_side_effect": False, "target_post_id": target_id}
            return self._execution(result, session, None)

        baseline_state = (
            baseline.data.get("post_state")
            if baseline.ok and isinstance(baseline.data, dict)
            else None
        )
        if baseline_state != "present":
            session.resolve_no_external_effect(
                reason=(
                    "delete_target_already_absent"
                    if baseline_state == "deleted"
                    else "delete_baseline_unresolved"
                )
            )
            if baseline_state == "deleted":
                result = soft_failure(
                    "approved delete target is already deleted; no mutation attempted",
                    failure_category=FailureCategory.SELECTOR_NOT_FOUND,
                )
            else:
                result = hard_failure(
                    "approved delete target presence could not be proven",
                    failure_category=FailureCategory.UNKNOWN,
                )
            result.data = {
                "public_side_effect": False,
                "target_post_id": target_id,
                "m5_effect_state": AttemptState.NO_EFFECT.value,
            }
            return self._execution(result, session, baseline)

        try:
            receipt = session.scope_effect()
        except ScopedAuthorityDenied as exc:
            session.resolve_no_external_effect(reason=f"delete_scope_denied:{exc.reason}")
            return self._clean_failure(session, f"delete authority denied: {exc.reason}")
        if not isinstance(receipt.authority, DeletePostAuthority):
            session.resolve_no_external_effect(reason="delete_authority_shape_mismatch")
            return self._clean_failure(session, "delete authority shape mismatch")

        try:
            mutation = await receipt.authority.delete()
        except BaseException as exc:
            return self._mutation_interrupted(session, receipt, exc, target_id)

        permit = receipt.permit
        if permit is None:
            if not session.attempt.reservation_started:
                session.resolve_no_external_effect(reason="delete_failed_before_commit_authority")
            return self._execution(mutation, session, None)
        if not permit.consumed:
            session.resolve_no_external_effect(reason="delete_permit_issued_but_not_consumed")
            return self._execution(mutation, session, None)

        try:
            verification = await self._evidence.read_delete_state(bound_url, target_id)
        except BaseException as exc:
            return self._verification_interrupted(session, mutation, exc, target_id)

        verified_state = (
            verification.data.get("post_state")
            if verification.ok and isinstance(verification.data, dict)
            else None
        )
        evidence = {
            "mutation_ok": bool(mutation.ok),
            "baseline_state": baseline_state,
            "verification_ok": bool(verification.ok),
            "verified_state": verified_state,
            "target_post_id": target_id,
            "verification_evidence": (
                verification.data.get("evidence")
                if verification.ok and isinstance(verification.data, dict)
                else None
            ),
        }
        if verification.ok and verified_state == "deleted":
            try:
                session.record_confirmed(evidence=evidence)
            except Exception as exc:  # noqa: BLE001
                return self._persistence_failure(session, exc)
            result = ok_result(
                data={
                    "result": "post_deleted_and_verified",
                    "target_post_id": target_id,
                    "post_state": "deleted",
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            )
            return self._execution(result, session, verification)

        try:
            session.record_unknown(evidence=evidence)
        except Exception as exc:  # noqa: BLE001
            return self._persistence_failure(session, exc)
        result = hard_failure(
            "delete authority crossed but deletion evidence is incomplete; reconciliation required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "target_post_id": target_id,
            "post_state": verified_state or "unknown",
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
        }
        return self._execution(result, session, verification)

    def _mutation_interrupted(
        self,
        session: M5ExecutionSession,
        receipt: AuthorizedEffect,
        exc: BaseException,
        target_id: str,
    ) -> M5DeleteExecution:
        permit = receipt.permit
        if permit is None:
            if not session.attempt.reservation_started:
                session.resolve_no_external_effect(reason="delete_interrupted_before_authority")
            result = hard_failure(
                f"delete interrupted before authority completed: {type(exc).__name__}",
                failure_category=FailureCategory.UNKNOWN,
            )
            result.data = {
                "public_side_effect": False,
                "target_post_id": target_id,
                "safety_state_unresolved": bool(session.attempt.reservation_started),
            }
            return self._execution(result, session, None)
        if not permit.consumed:
            session.resolve_no_external_effect(reason="delete_interrupted_before_consumption")
            result = hard_failure(
                f"delete interrupted before permit consumption: {type(exc).__name__}",
                failure_category=FailureCategory.SECURITY,
            )
            result.data = {"public_side_effect": False, "target_post_id": target_id}
            return self._execution(result, session, None)

        try:
            session.record_unknown(
                evidence={
                    "phase": "delete_mutation",
                    "exception_type": type(exc).__name__,
                    "target_post_id": target_id,
                }
            )
        except Exception as persist_exc:  # noqa: BLE001
            return self._persistence_failure(session, persist_exc)
        result = hard_failure(
            "delete interrupted after authority crossed; reconciliation required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "target_post_id": target_id,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
        }
        return self._execution(result, session, None)

    def _verification_interrupted(
        self,
        session: M5ExecutionSession,
        mutation: ActionResult,
        exc: BaseException,
        target_id: str,
    ) -> M5DeleteExecution:
        try:
            session.record_unknown(
                evidence={
                    "phase": "delete_verification",
                    "exception_type": type(exc).__name__,
                    "mutation_ok": bool(mutation.ok),
                    "target_post_id": target_id,
                }
            )
        except Exception as persist_exc:  # noqa: BLE001
            return self._persistence_failure(session, persist_exc)
        result = hard_failure(
            "delete verification interrupted after authority crossed; reconciliation required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "target_post_id": target_id,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
        }
        return self._execution(result, session, None)

    def _persistence_failure(
        self,
        session: M5ExecutionSession,
        exc: Exception,
    ) -> M5DeleteExecution:
        result = hard_failure(
            f"could not persist terminal M5 delete outcome ({type(exc).__name__})",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": session.attempt.state.value,
            "terminal_persistence_failed": True,
        }
        return self._execution(result, session, None)

    def _clean_failure(self, session: M5ExecutionSession, message: str) -> M5DeleteExecution:
        return self._execution(
            hard_failure(message, failure_category=FailureCategory.SECURITY),
            session,
            None,
        )

    @staticmethod
    def _execution(
        result: ActionResult,
        session: M5ExecutionSession,
        verification: Optional[ActionResult],
    ) -> M5DeleteExecution:
        receipt = session.authorized_effect
        permit = receipt.permit if receipt is not None else None
        return M5DeleteExecution(
            result=result,
            verification=verification,
            attempt_state=session.attempt.state,
            permit_issued=permit is not None,
            permit_consumed=bool(permit is not None and permit.consumed),
        )
