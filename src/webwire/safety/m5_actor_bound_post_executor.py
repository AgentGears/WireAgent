"""Actor-bound plain-post terminalization for the supported live M5 path."""

from __future__ import annotations

from typing import Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_actor_bound_evidence import status_url_identity
from webwire.safety.m5_execution_runtime import M5ExecutionSession
from webwire.safety.m5_post_text_executor import M5PostTextExecution, M5PostTextExecutor
from webwire.safety.scoped_authority import AuthorizedEffect

__all__ = ["M5ActorBoundPostTextExecutor"]


class M5ActorBoundPostTextExecutor(M5PostTextExecutor):
    """Require actor/ID/direct-status evidence before ``EFFECT_CONFIRMED``."""

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

        permit = receipt.permit
        approved_actor = (
            permit.actor_id.lstrip("@").casefold()
            if permit is not None and permit.consumed
            else ""
        )
        verification_data = (
            verification.data
            if verification is not None
            and verification.ok
            and isinstance(verification.data, dict)
            else {}
        )
        observed_actor = verification_data.get("post_actor")
        observed_id = verification_data.get("post_id")
        verified_url = verification_data.get("post_url")
        captured_identity = (
            status_url_identity(post_url) if isinstance(post_url, str) else None
        )
        verified_identity = (
            status_url_identity(verified_url) if isinstance(verified_url, str) else None
        )
        actor_verified = bool(
            approved_actor
            and isinstance(observed_actor, str)
            and observed_actor.lstrip("@").casefold() == approved_actor
        )
        status_verified = bool(
            isinstance(post_id, str)
            and post_id.isdigit()
            and observed_id == post_id
            and verification_data.get("direct_status_owned") is True
            and verification_data.get("text_matches") is True
            and captured_identity == (approved_actor, post_id)
            and verified_identity == (approved_actor, post_id)
        )
        confirmed = bool(
            verification is not None
            and verification.ok
            and actor_verified
            and status_verified
        )
        evidence = {
            "submit_result_ok": bool(submit_result.ok),
            "posted_post_id": post_id,
            "captured_url": post_url,
            "verified_url": verified_url,
            "approved_actor": approved_actor,
            "observed_actor": observed_actor,
            "actor_verified": actor_verified,
            "direct_status_owned": verification_data.get("direct_status_owned") is True,
            "text_verified": verification_data.get("text_matches") is True,
            "status_identity_verified": status_verified,
        }

        if confirmed:
            try:
                session.record_confirmed(evidence=evidence)
            except Exception as exc:  # noqa: BLE001
                return self._unresolved_persistence_failure(session, exc)
            result = ok_result(
                data={
                    "result": "posted_and_verified",
                    "posted_url": verified_url,
                    "posted_post_id": post_id,
                    "submitted_text": normalized,
                    "actor_verified": True,
                    "direct_status_owned": True,
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            )
            return self._execution(result, session, verification)

        try:
            session.record_unknown(evidence=evidence)
        except Exception as exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, exc)
        result = hard_failure(
            "post submit crossed authority but actor-bound direct status evidence was "
            "incomplete; reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "posted_url": verified_url or post_url,
            "posted_post_id": post_id,
            "submitted_text": normalized,
            "actor_verified": actor_verified,
            "direct_status_owned": verification_data.get("direct_status_owned") is True,
        }
        return self._execution(result, session, verification or capture)
