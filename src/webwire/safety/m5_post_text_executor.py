"""Layer-5 scoped execution for the first irreversible content canary.

``post_text`` is migrated before reply/quote/media so the content protocol can
be proven without target-thread or attachment dimensions. The executor owns one
frozen copy of the confirmed intent, stages only through
``PostPreparationAuthority``, crosses one ``SubmitContentAuthority`` seam, and
records terminal effect truth only from post-submit evidence.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime, M5ExecutionSession
from webwire.safety.models import WriteIntent
from webwire.safety.scoped_authority import (
    AuthorizedEffect,
    PostPreparationAuthority,
    ScopedAuthorityDenied,
)
from webwire.safety.text_normalize import normalize_text, validate_length

__all__ = [
    "M5PostTextEvidence",
    "M5PostTextExecution",
    "M5PostTextExecutor",
]


@runtime_checkable
class M5PostTextEvidence(Protocol):
    async def capture_pre_submit_ids(self) -> ActionResult: ...

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult: ...

    async def verify_post_text(
        self,
        post_url: str,
        normalized_text: str,
    ) -> ActionResult: ...


@dataclass(frozen=True)
class M5PostTextExecution:
    result: ActionResult
    verification: Optional[ActionResult]
    attempt_state: AttemptState
    permit_issued: bool
    permit_consumed: bool


class M5PostTextExecutor:
    """Stage, submit, and evidence-terminalize one approved plain text post."""

    def __init__(
        self,
        *,
        runtime: M5ExecutionRuntime,
        evidence_reader: M5PostTextEvidence,
    ) -> None:
        self._runtime = runtime
        self._evidence = evidence_reader

    async def execute(self, intent: WriteIntent) -> M5PostTextExecution:
        # Freeze before handing anything to the runtime and never re-read the
        # caller-owned intent after this point.
        frozen = deepcopy(intent)
        session = self._runtime.issue(frozen)
        if session.action_type != "post":
            session.resolve_no_external_effect(reason="post_text_executor_wrong_action")
            return self._clean_failure(
                session,
                "post_text executor requires action_type='post'",
            )

        normalized = frozen.payload.get("normalized_text", "")
        if not isinstance(normalized, str):
            session.resolve_no_external_effect(reason="post_text_payload_invalid")
            return self._clean_failure(session, "approved normalized_text is invalid")
        valid, char_count = validate_length(normalized)
        if not valid or not normalized:
            session.resolve_no_external_effect(reason="post_text_validation_failed")
            return self._clean_failure(
                session,
                f"approved post text is empty or invalid ({char_count} chars)",
            )

        try:
            preparation = session.prepare()
        except ScopedAuthorityDenied as exc:
            session.resolve_no_external_effect(reason=f"preparation_denied:{exc.reason}")
            return self._clean_failure(session, f"post preparation denied: {exc.reason}")
        if not isinstance(preparation, PostPreparationAuthority):
            session.resolve_no_external_effect(reason="preparation_shape_mismatch")
            return self._clean_failure(session, "post preparation authority shape mismatch")

        fill = await preparation.fill_composer(normalized)
        if not fill.ok:
            await self._abort_precommit(
                session,
                preparation,
                reason="fill_composer_failed",
            )
            return self._execution(fill, session, None)

        readback = await preparation.read_composer_text()
        composer_text = (
            (readback.data or {}).get("composer_text", "")
            if readback.ok and isinstance(readback.data, dict)
            else ""
        )
        if not readback.ok or normalize_text(composer_text) != normalize_text(normalized):
            await self._abort_precommit(
                session,
                preparation,
                reason="composer_readback_mismatch",
            )
            return self._execution(
                hard_failure(
                    "approved composer text could not be re-verified before submit",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                readback,
            )

        try:
            baseline = await self._evidence.capture_pre_submit_ids()
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="pre_submit_identity_baseline_interrupted",
            )
            return self._execution(
                hard_failure(
                    "pre-submit identity baseline was interrupted before commit "
                    f"authority: {type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
            )
        if not baseline.ok or not isinstance(baseline.data, dict):
            await self._abort_precommit(
                session,
                preparation,
                reason="pre_submit_identity_baseline_failed",
            )
            return self._execution(
                hard_failure(
                    "could not establish pre-submit status identity baseline",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                baseline,
            )
        raw_ids = baseline.data.get("status_ids", [])
        if not isinstance(raw_ids, list) or not all(isinstance(v, str) for v in raw_ids):
            await self._abort_precommit(
                session,
                preparation,
                reason="pre_submit_identity_baseline_invalid",
            )
            return self._execution(
                hard_failure(
                    "pre-submit status identity baseline was malformed",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                baseline,
            )
        pre_submit_ids = set(raw_ids)

        try:
            receipt = session.scope_effect()
        except ScopedAuthorityDenied as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason=f"scope_effect_denied:{exc.reason}",
            )
            return self._execution(
                hard_failure(
                    f"post submit authority denied: {exc.reason}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
            )

        try:
            submit_result = await receipt.authority.submit()  # type: ignore[union-attr]
        except BaseException as exc:
            return await self._interrupted_submit(
                session,
                preparation,
                receipt,
                exc,
            )

        permit = receipt.permit
        if permit is None:
            # Even an ambiguous reservation failure must release transient
            # browser/composer ownership. Only the *effect attempt* remains
            # unresolved; UI cleanup does not prove or rewrite ledger truth.
            try:
                await preparation.close_composer()
            finally:
                if not session.attempt.reservation_started:
                    session.resolve_no_external_effect(
                        reason="submit_failed_before_commit_authority"
                    )
            return self._execution(submit_result, session, None)

        if not permit.consumed:
            # The canonical click did not cross. Browser cleanup and authority
            # closure are independent safety duties; failure to clean the UI
            # must not resurrect an already-spent approval.
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="submit_permit_issued_but_not_consumed"
                )
            return self._execution(submit_result, session, None)

        return await self._terminalize_consumed(
            session,
            receipt,
            submit_result,
            pre_submit_ids,
            normalized,
        )

    async def _terminalize_consumed(
        self,
        session: M5ExecutionSession,
        receipt: AuthorizedEffect,
        submit_result: ActionResult,
        pre_submit_ids: set[str],
        normalized: str,
    ) -> M5PostTextExecution:
        try:
            capture = await self._evidence.capture_new_post(pre_submit_ids)
        except BaseException as exc:
            return self._post_submit_evidence_interrupted(
                session,
                submit_result,
                phase="capture_new_post",
                exc=exc,
            )
        capture_data = capture.data if capture.ok and isinstance(capture.data, dict) else {}
        post_id = capture_data.get("post_id")
        post_url = capture_data.get("post_url")

        verification: Optional[ActionResult] = None
        if isinstance(post_id, str) and post_id and isinstance(post_url, str) and post_url:
            try:
                verification = await self._evidence.verify_post_text(post_url, normalized)
            except BaseException as exc:
                return self._post_submit_evidence_interrupted(
                    session,
                    submit_result,
                    phase="verify_post_text",
                    exc=exc,
                    post_id=post_id,
                    post_url=post_url,
                )

        confirmed = bool(
            post_id
            and post_url
            and verification is not None
            and verification.ok
        )
        evidence = {
            "submit_result_ok": bool(submit_result.ok),
            "posted_post_id": post_id,
            "posted_url": post_url,
            "text_verified": bool(verification is not None and verification.ok),
        }

        if confirmed:
            try:
                session.record_confirmed(evidence=evidence)
            except Exception as exc:  # noqa: BLE001
                return self._unresolved_persistence_failure(session, exc)
            result = ok_result(
                data={
                    "result": "posted_and_verified",
                    "posted_url": post_url,
                    "posted_post_id": post_id,
                    "submitted_text": normalized,
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            )
            return self._execution(result, session, verification)

        try:
            session.record_unknown(evidence=evidence)
        except Exception as exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, exc)
        result = hard_failure(
            "post submit crossed authority but could not be independently verified; "
            "reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "posted_url": post_url,
            "posted_post_id": post_id,
            "submitted_text": normalized,
        }
        return self._execution(result, session, verification or capture)

    def _post_submit_evidence_interrupted(
        self,
        session: M5ExecutionSession,
        submit_result: ActionResult,
        *,
        phase: str,
        exc: BaseException,
        post_id: Optional[str] = None,
        post_url: Optional[str] = None,
    ) -> M5PostTextExecution:
        evidence = {
            "phase": phase,
            "exception_type": type(exc).__name__,
            "submit_result_ok": bool(submit_result.ok),
            "posted_post_id": post_id,
            "posted_url": post_url,
        }
        try:
            session.record_unknown(evidence=evidence)
        except Exception as persist_exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, persist_exc)
        result = hard_failure(
            "post-submit evidence was interrupted after authority crossed; "
            "reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "evidence_phase": phase,
            "posted_url": post_url,
            "posted_post_id": post_id,
        }
        return self._execution(result, session, None)

    async def _abort_precommit(
        self,
        session: M5ExecutionSession,
        preparation: PostPreparationAuthority,
        *,
        reason: str,
    ) -> None:
        try:
            await preparation.close_composer()
        finally:
            if not session.attempt.reservation_started:
                session.resolve_no_external_effect(reason=reason)

    async def _interrupted_submit(
        self,
        session: M5ExecutionSession,
        preparation: PostPreparationAuthority,
        receipt: AuthorizedEffect,
        exc: BaseException,
    ) -> M5PostTextExecution:
        permit = receipt.permit
        if permit is None:
            try:
                await preparation.close_composer()
            finally:
                if not session.attempt.reservation_started:
                    session.resolve_no_external_effect(
                        reason="submit_interrupted_before_commit_authority"
                    )
            result = hard_failure(
                f"post submit interrupted before commit authority: {type(exc).__name__}",
                failure_category=FailureCategory.UNKNOWN,
            )
            return self._execution(result, session, None)

        if not permit.consumed:
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="submit_interrupted_before_permit_consumption"
                )
            result = hard_failure(
                f"post submit interrupted before permit consumption: {type(exc).__name__}",
                failure_category=FailureCategory.SECURITY,
            )
            return self._execution(result, session, None)

        try:
            session.record_unknown(
                evidence={
                    "phase": "submit",
                    "exception_type": type(exc).__name__,
                }
            )
        except Exception as persist_exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, persist_exc)
        result = hard_failure(
            "post submit was interrupted after authority crossed; reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
        }
        return self._execution(result, session, None)

    def _unresolved_persistence_failure(
        self,
        session: M5ExecutionSession,
        exc: Exception,
    ) -> M5PostTextExecution:
        result = hard_failure(
            "could not persist the terminal M5 content outcome; safety state remains "
            f"unresolved ({type(exc).__name__})",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": session.attempt.state.value,
            "terminal_persistence_failed": True,
        }
        return self._execution(result, session, None)

    def _clean_failure(
        self,
        session: M5ExecutionSession,
        message: str,
    ) -> M5PostTextExecution:
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
    ) -> M5PostTextExecution:
        receipt = session.authorized_effect
        permit = receipt.permit if receipt is not None else None
        return M5PostTextExecution(
            result=result,
            verification=verification,
            attempt_state=session.attempt.state,
            permit_issued=permit is not None,
            permit_consumed=bool(permit is not None and permit.consumed),
        )
