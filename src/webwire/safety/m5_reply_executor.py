"""Layer-5 scoped execution for target-bound plain-text replies.

The reply executor mirrors the proven ``post_text`` lifecycle but adds one
approved target dimension. It stages only through ``ReplyPreparationAuthority``,
crosses one ``SubmitContentAuthority`` seam, and records ``EFFECT_CONFIRMED``
only after post-submit evidence proves the captured status belongs to the
approved target thread, actor, and text.
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
    ReplyPreparationAuthority,
    ScopedAuthorityDenied,
)
from webwire.safety.text_normalize import normalize_text, validate_length

__all__ = [
    "M5ReplyEvidence",
    "M5ReplyExecution",
    "M5ReplyExecutor",
]


@runtime_checkable
class M5ReplyEvidence(Protocol):
    async def capture_pre_submit_ids(self) -> ActionResult: ...

    async def capture_new_post(
        self,
        pre_submit_ids: set[str],
        *,
        exclude_ids: set[str] | None = None,
    ) -> ActionResult: ...

    async def verify_reply_in_thread(
        self,
        *,
        target_post_id: str,
        reply_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult: ...


@dataclass(frozen=True)
class M5ReplyExecution:
    result: ActionResult
    verification: Optional[ActionResult]
    attempt_state: AttemptState
    permit_issued: bool
    permit_consumed: bool


class M5ReplyExecutor:
    """Stage, submit, and evidence-terminalize one approved plain-text reply."""

    def __init__(
        self,
        *,
        runtime: M5ExecutionRuntime,
        evidence_reader: M5ReplyEvidence,
    ) -> None:
        self._runtime = runtime
        self._evidence = evidence_reader

    async def execute(self, intent: WriteIntent) -> M5ReplyExecution:
        frozen = deepcopy(intent)
        session = self._runtime.issue(frozen)
        if session.action_type != "reply":
            session.resolve_no_external_effect(reason="reply_executor_wrong_action")
            return self._clean_failure(
                session,
                "reply executor requires action_type='reply'",
            )

        normalized = frozen.payload.get("normalized_text", "")
        target_post_id = frozen.payload.get("target_post_id", "")
        post_url = frozen.payload.get("post_url", "")
        if not isinstance(normalized, str):
            session.resolve_no_external_effect(reason="reply_payload_text_invalid")
            return self._clean_failure(session, "approved normalized_text is invalid")
        if not isinstance(target_post_id, str) or not target_post_id.isdigit():
            session.resolve_no_external_effect(reason="reply_payload_target_invalid")
            return self._clean_failure(session, "approved reply target_post_id is invalid")
        if not isinstance(post_url, str):
            session.resolve_no_external_effect(reason="reply_payload_url_invalid")
            return self._clean_failure(session, "approved reply post_url is invalid")
        bound_post_url = post_url or f"https://x.com/i/status/{target_post_id}"

        valid, char_count = validate_length(normalized)
        if not valid or not normalized:
            session.resolve_no_external_effect(reason="reply_validation_failed")
            return self._clean_failure(
                session,
                f"approved reply text is empty or invalid ({char_count} chars)",
            )

        try:
            preparation = session.prepare()
        except ScopedAuthorityDenied as exc:
            session.resolve_no_external_effect(reason=f"preparation_denied:{exc.reason}")
            return self._clean_failure(session, f"reply preparation denied: {exc.reason}")
        if not isinstance(preparation, ReplyPreparationAuthority):
            session.resolve_no_external_effect(reason="preparation_shape_mismatch")
            return self._clean_failure(session, "reply preparation authority shape mismatch")

        try:
            opened = await preparation.open_reply_on_target(bound_post_url, target_post_id)
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="open_reply_interrupted",
            )
            return self._execution(
                hard_failure(
                    "reply target preparation was interrupted before commit authority: "
                    f"{type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
            )
        if not opened.ok:
            await self._abort_precommit(
                session,
                preparation,
                reason="open_reply_failed",
            )
            return self._execution(opened, session, None)

        try:
            fill = await preparation.fill_reply_composer(normalized)
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="fill_reply_interrupted",
            )
            return self._execution(
                hard_failure(
                    "reply composer fill was interrupted before commit authority: "
                    f"{type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
            )
        if not fill.ok:
            await self._abort_precommit(
                session,
                preparation,
                reason="fill_reply_failed",
            )
            return self._execution(fill, session, None)

        try:
            readback = await preparation.read_composer_text()
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="reply_composer_readback_interrupted",
            )
            return self._execution(
                hard_failure(
                    "reply composer readback was interrupted before commit authority: "
                    f"{type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
            )
        composer_text = (
            (readback.data or {}).get("composer_text", "")
            if readback.ok and isinstance(readback.data, dict)
            else ""
        )
        if not readback.ok or normalize_text(composer_text) != normalize_text(normalized):
            await self._abort_precommit(
                session,
                preparation,
                reason="reply_composer_readback_mismatch",
            )
            return self._execution(
                hard_failure(
                    "approved reply composer text could not be re-verified before submit",
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
                reason="reply_pre_submit_identity_baseline_interrupted",
            )
            return self._execution(
                hard_failure(
                    "reply pre-submit identity baseline was interrupted before commit "
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
                reason="reply_pre_submit_identity_baseline_failed",
            )
            return self._execution(
                hard_failure(
                    "could not establish reply pre-submit status identity baseline",
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
                reason="reply_pre_submit_identity_baseline_invalid",
            )
            return self._execution(
                hard_failure(
                    "reply pre-submit status identity baseline was malformed",
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
                    f"reply submit authority denied: {exc.reason}",
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
            try:
                await preparation.close_composer()
            finally:
                if not session.attempt.reservation_started:
                    session.resolve_no_external_effect(
                        reason="reply_submit_failed_before_commit_authority"
                    )
            return self._execution(submit_result, session, None)

        if not permit.consumed:
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="reply_submit_permit_issued_but_not_consumed"
                )
            return self._execution(submit_result, session, None)

        return await self._terminalize_consumed(
            session,
            submit_result,
            pre_submit_ids,
            normalized,
            target_post_id,
            frozen.actor_identity or "",
        )

    async def _terminalize_consumed(
        self,
        session: M5ExecutionSession,
        submit_result: ActionResult,
        pre_submit_ids: set[str],
        normalized: str,
        target_post_id: str,
        actor_id: str,
    ) -> M5ReplyExecution:
        try:
            capture = await self._evidence.capture_new_post(
                pre_submit_ids,
                exclude_ids={target_post_id},
            )
        except BaseException as exc:
            return self._post_submit_evidence_interrupted(
                session,
                submit_result,
                phase="capture_reply_status",
                exc=exc,
                target_post_id=target_post_id,
            )
        capture_data = capture.data if capture.ok and isinstance(capture.data, dict) else {}
        reply_post_id = capture_data.get("post_id")
        captured_url = capture_data.get("post_url")

        verification: Optional[ActionResult] = None
        if isinstance(reply_post_id, str) and reply_post_id:
            try:
                verification = await self._evidence.verify_reply_in_thread(
                    target_post_id=target_post_id,
                    reply_post_id=reply_post_id,
                    actor_id=actor_id,
                    normalized_text=normalized,
                )
            except BaseException as exc:
                return self._post_submit_evidence_interrupted(
                    session,
                    submit_result,
                    phase="verify_reply_in_thread",
                    exc=exc,
                    target_post_id=target_post_id,
                    reply_post_id=reply_post_id,
                    reply_url=(captured_url if isinstance(captured_url, str) else None),
                )

        verification_data = (
            verification.data
            if verification is not None
            and verification.ok
            and isinstance(verification.data, dict)
            else {}
        )
        verified_url = verification_data.get("reply_url")
        reply_actor = verification_data.get("reply_actor")
        approved_actor = actor_id.lstrip("@").casefold()
        reply_id_valid = bool(
            isinstance(reply_post_id, str)
            and reply_post_id.isdigit()
            and reply_post_id != target_post_id
        )
        actor_verified = bool(
            isinstance(reply_actor, str)
            and reply_actor.lstrip("@").casefold() == approved_actor
        )
        url_verified = bool(
            reply_id_valid
            and isinstance(verified_url, str)
            and f"/status/{reply_post_id}" in verified_url
        )
        confirmed = bool(
            reply_id_valid
            and verification is not None
            and verification.ok
            and verification_data.get("thread_bound") is True
            and verification_data.get("text_matches") is True
            and verification_data.get("target_post_id") == target_post_id
            and verification_data.get("reply_post_id") == reply_post_id
            and actor_verified
            and url_verified
        )
        evidence = {
            "submit_result_ok": bool(submit_result.ok),
            "target_post_id": target_post_id,
            "reply_post_id": reply_post_id,
            "captured_url": captured_url,
            "verified_reply_url": verified_url,
            "thread_bound": bool(verification_data.get("thread_bound") is True),
            "text_verified": bool(verification_data.get("text_matches") is True),
            "actor_verified": actor_verified,
            "url_verified": url_verified,
        }

        if confirmed:
            try:
                session.record_confirmed(evidence=evidence)
            except Exception as exc:  # noqa: BLE001
                return self._unresolved_persistence_failure(session, exc)
            result = ok_result(
                data={
                    "result": "reply_posted_and_target_verified",
                    "posted_url": verified_url,
                    "posted_post_id": reply_post_id,
                    "target_post_id": target_post_id,
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
            "reply submit crossed authority but target-thread evidence was incomplete; "
            "reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "target_post_id": target_post_id,
            "posted_url": verified_url or captured_url,
            "posted_post_id": reply_post_id,
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
        target_post_id: str,
        reply_post_id: Optional[str] = None,
        reply_url: Optional[str] = None,
    ) -> M5ReplyExecution:
        evidence = {
            "phase": phase,
            "exception_type": type(exc).__name__,
            "submit_result_ok": bool(submit_result.ok),
            "target_post_id": target_post_id,
            "reply_post_id": reply_post_id,
            "reply_url": reply_url,
        }
        try:
            session.record_unknown(evidence=evidence)
        except Exception as persist_exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, persist_exc)
        result = hard_failure(
            "reply post-submit evidence was interrupted after authority crossed; "
            "reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "evidence_phase": phase,
            "target_post_id": target_post_id,
            "posted_url": reply_url,
            "posted_post_id": reply_post_id,
        }
        return self._execution(result, session, None)

    async def _abort_precommit(
        self,
        session: M5ExecutionSession,
        preparation: ReplyPreparationAuthority,
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
        preparation: ReplyPreparationAuthority,
        receipt: AuthorizedEffect,
        exc: BaseException,
    ) -> M5ReplyExecution:
        permit = receipt.permit
        if permit is None:
            try:
                await preparation.close_composer()
            finally:
                if not session.attempt.reservation_started:
                    session.resolve_no_external_effect(
                        reason="reply_submit_interrupted_before_commit_authority"
                    )
            result = hard_failure(
                "reply submit interrupted before commit authority: "
                f"{type(exc).__name__}",
                failure_category=FailureCategory.UNKNOWN,
            )
            return self._execution(result, session, None)

        if not permit.consumed:
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="reply_submit_interrupted_before_permit_consumption"
                )
            result = hard_failure(
                "reply submit interrupted before permit consumption: "
                f"{type(exc).__name__}",
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
            "reply submit was interrupted after authority crossed; reconciliation is required",
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
    ) -> M5ReplyExecution:
        result = hard_failure(
            "could not persist the terminal M5 reply outcome; safety state remains "
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
    ) -> M5ReplyExecution:
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
    ) -> M5ReplyExecution:
        receipt = session.authorized_effect
        permit = receipt.permit if receipt is not None else None
        return M5ReplyExecution(
            result=result,
            verification=verification,
            attempt_state=session.attempt.state,
            permit_issued=permit is not None,
            permit_consumed=bool(permit is not None and permit.consumed),
        )
