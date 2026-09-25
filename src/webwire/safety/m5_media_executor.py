"""Layer-5 scoped execution for approved media content.

One executor covers post/reply/quote media capabilities because the authority
protocol is identical after context establishment: bind one ordered manifest,
stage each attachment through the scoped preparation authority, cross one
``SubmitContentAuthority`` seam, and terminalize the exact receipt from
post-submit identity/content/attachment-count evidence.

The executor intentionally does not claim source-byte equivalence after remote
transcoding. The approved source digest is rechecked immediately before each
attach operation by Layer 4; post-submit evidence proves only the resulting
attachment count on the exact captured status.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, hard_failure, ok_result
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_execution_runtime import M5ExecutionRuntime, M5ExecutionSession
from webwire.safety.m5_media_evidence import M5MediaCountEvidence
from webwire.safety.models import WriteIntent
from webwire.safety.scoped_authority import (
    AuthorizedEffect,
    PostPreparationAuthority,
    QuotePreparationAuthority,
    ReplyPreparationAuthority,
    ScopedAuthorityDenied,
)
from webwire.safety.text_normalize import normalize_text, validate_length

__all__ = [
    "M5MediaContentEvidence",
    "M5MediaExecution",
    "M5MediaExecutor",
]


@runtime_checkable
class M5MediaContentEvidence(Protocol):
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

    async def verify_reply_in_thread(
        self,
        *,
        target_post_id: str,
        reply_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult: ...

    async def verify_quote_attachment(
        self,
        *,
        quote_post_id: str,
        target_post_id: str,
        actor_id: str,
        normalized_text: str,
    ) -> ActionResult: ...


PreparationAuthority = (
    PostPreparationAuthority | ReplyPreparationAuthority | QuotePreparationAuthority
)


@dataclass(frozen=True)
class M5MediaExecution:
    result: ActionResult
    verification: Optional[ActionResult]
    media_verification: Optional[ActionResult]
    attempt_state: AttemptState
    permit_issued: bool
    permit_consumed: bool


class M5MediaExecutor:
    """Execute one approved post/reply/quote carrying one or more media items."""

    def __init__(
        self,
        *,
        runtime: M5ExecutionRuntime,
        content_evidence: M5MediaContentEvidence,
        media_evidence: M5MediaCountEvidence,
    ) -> None:
        self._runtime = runtime
        self._content_evidence = content_evidence
        self._media_evidence = media_evidence

    async def execute(self, intent: WriteIntent) -> M5MediaExecution:
        frozen = deepcopy(intent)
        session = self._runtime.issue(frozen)
        action_type = session.action_type
        if action_type not in {"post", "reply", "quote"}:
            session.resolve_no_external_effect(reason="media_executor_wrong_action")
            return self._clean_failure(
                session,
                "media executor requires action_type post/reply/quote",
            )

        normalized = frozen.payload.get("normalized_text", "")
        if not isinstance(normalized, str):
            session.resolve_no_external_effect(reason="media_payload_text_invalid")
            return self._clean_failure(session, "approved normalized_text is invalid")
        valid, char_count = validate_length(normalized)
        if not valid:
            session.resolve_no_external_effect(reason="media_text_validation_failed")
            return self._clean_failure(
                session,
                f"approved media text is invalid ({char_count} chars)",
            )

        manifest = frozen.payload.get("manifest_items", [])
        expected_count = frozen.payload.get("image_count", len(manifest) if isinstance(manifest, list) else 0)
        if (
            not isinstance(manifest, list)
            or not manifest
            or not isinstance(expected_count, int)
            or expected_count <= 0
            or expected_count != len(manifest)
        ):
            session.resolve_no_external_effect(reason="media_manifest_invalid")
            return self._clean_failure(session, "approved media manifest/count is invalid")

        media_paths: list[str] = []
        media_digests: list[str] = []
        for index, item in enumerate(manifest):
            if not isinstance(item, dict) or item.get("index") != index:
                session.resolve_no_external_effect(reason="media_manifest_order_invalid")
                return self._clean_failure(session, "approved media manifest order is invalid")
            source_path = item.get("source_path")
            digest = item.get("sha256")
            if not isinstance(source_path, str) or not source_path:
                session.resolve_no_external_effect(reason="media_manifest_path_invalid")
                return self._clean_failure(session, "approved media source path is invalid")
            if not isinstance(digest, str) or len(digest) != 64:
                session.resolve_no_external_effect(reason="media_manifest_digest_invalid")
                return self._clean_failure(session, "approved media digest is invalid")
            media_paths.append(source_path)
            media_digests.append(digest.lower())

        target_post_id = ""
        bound_post_url = ""
        if action_type in {"reply", "quote"}:
            raw_target = frozen.payload.get("target_post_id", "")
            raw_url = frozen.payload.get("post_url", "")
            if not isinstance(raw_target, str) or not raw_target.isdigit():
                session.resolve_no_external_effect(reason="media_target_invalid")
                return self._clean_failure(session, "approved media target_post_id is invalid")
            if not isinstance(raw_url, str):
                session.resolve_no_external_effect(reason="media_target_url_invalid")
                return self._clean_failure(session, "approved media target URL is invalid")
            target_post_id = raw_target
            bound_post_url = raw_url or f"https://x.com/i/status/{target_post_id}"

        try:
            preparation = session.prepare()
        except ScopedAuthorityDenied as exc:
            session.resolve_no_external_effect(reason=f"preparation_denied:{exc.reason}")
            return self._clean_failure(session, f"media preparation denied: {exc.reason}")

        staged = await self._stage_context_and_text(
            session,
            preparation,
            action_type=action_type,
            normalized=normalized,
            post_url=bound_post_url,
            target_post_id=target_post_id,
        )
        if staged is not None:
            return staged

        for index, source_path in enumerate(media_paths):
            try:
                attached = await preparation.attach_media(source_path)
            except BaseException as exc:
                await self._abort_precommit(
                    session,
                    preparation,
                    reason=f"media_attach_{index}_interrupted",
                )
                return self._execution(
                    hard_failure(
                        "media attachment was interrupted before commit authority: "
                        f"{type(exc).__name__}",
                        failure_category=FailureCategory.SECURITY,
                    ),
                    session,
                    None,
                    None,
                )
            if not attached.ok:
                await self._abort_precommit(
                    session,
                    preparation,
                    reason=f"media_attach_{index}_failed",
                )
                return self._execution(attached, session, None, None)

            try:
                ready = await preparation.verify_attachment_ready()
                count = await preparation.count_attachments()
            except BaseException as exc:
                await self._abort_precommit(
                    session,
                    preparation,
                    reason=f"media_attachment_{index}_proof_interrupted",
                )
                return self._execution(
                    hard_failure(
                        "media attachment readiness/count proof was interrupted before "
                        f"commit authority: {type(exc).__name__}",
                        failure_category=FailureCategory.SECURITY,
                    ),
                    session,
                    None,
                    None,
                )
            observed = (
                count.data.get("count")
                if count.ok and isinstance(count.data, dict)
                else None
            )
            if not ready.ok or observed != index + 1:
                await self._abort_precommit(
                    session,
                    preparation,
                    reason=f"media_attachment_{index}_proof_failed",
                )
                return self._execution(
                    hard_failure(
                        f"approved attachment {index} was not uniquely ready at count {index + 1}",
                        failure_category=FailureCategory.SECURITY,
                    ),
                    session,
                    ready if not ready.ok else count,
                    None,
                )

        try:
            readback = await preparation.read_composer_text()
            final_count = await preparation.count_attachments()
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="media_composition_recheck_interrupted",
            )
            return self._execution(
                hard_failure(
                    "media composition recheck was interrupted before commit authority: "
                    f"{type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
                None,
            )
        composer_text = (
            readback.data.get("composer_text", "")
            if readback.ok and isinstance(readback.data, dict)
            else ""
        )
        final_observed = (
            final_count.data.get("count")
            if final_count.ok and isinstance(final_count.data, dict)
            else None
        )
        if (
            not readback.ok
            or normalize_text(composer_text) != normalize_text(normalized)
            or final_observed != expected_count
        ):
            await self._abort_precommit(
                session,
                preparation,
                reason="media_composition_recheck_failed",
            )
            return self._execution(
                hard_failure(
                    "approved text/media composition could not be re-verified before submit",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                readback if not readback.ok else final_count,
                None,
            )

        try:
            baseline = await self._content_evidence.capture_pre_submit_ids()
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="media_pre_submit_identity_baseline_interrupted",
            )
            return self._execution(
                hard_failure(
                    "media pre-submit identity baseline was interrupted before commit "
                    f"authority: {type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
                None,
            )
        if not baseline.ok or not isinstance(baseline.data, dict):
            await self._abort_precommit(
                session,
                preparation,
                reason="media_pre_submit_identity_baseline_failed",
            )
            return self._execution(
                hard_failure(
                    "could not establish media pre-submit status identity baseline",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                baseline,
                None,
            )
        raw_ids = baseline.data.get("status_ids", [])
        if not isinstance(raw_ids, list) or not all(isinstance(value, str) for value in raw_ids):
            await self._abort_precommit(
                session,
                preparation,
                reason="media_pre_submit_identity_baseline_invalid",
            )
            return self._execution(
                hard_failure(
                    "media pre-submit status identity baseline was malformed",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                baseline,
                None,
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
                    f"media submit authority denied: {exc.reason}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
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
                        reason="media_submit_failed_before_commit_authority"
                    )
            return self._execution(submit_result, session, None, None)

        if not permit.consumed:
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="media_submit_permit_issued_but_not_consumed"
                )
            return self._execution(submit_result, session, None, None)

        return await self._terminalize_consumed(
            session,
            submit_result,
            pre_submit_ids=pre_submit_ids,
            action_type=action_type,
            normalized=normalized,
            target_post_id=target_post_id,
            actor_id=frozen.actor_identity or "",
            expected_count=expected_count,
            media_digests=media_digests,
        )

    async def _stage_context_and_text(
        self,
        session: M5ExecutionSession,
        preparation: PreparationAuthority,
        *,
        action_type: str,
        normalized: str,
        post_url: str,
        target_post_id: str,
    ) -> Optional[M5MediaExecution]:
        try:
            if action_type == "post" and isinstance(preparation, PostPreparationAuthority):
                result = await preparation.fill_composer(normalized)
            elif action_type == "reply" and isinstance(preparation, ReplyPreparationAuthority):
                opened = await preparation.open_reply_on_target(post_url, target_post_id)
                if not opened.ok:
                    await self._abort_precommit(
                        session,
                        preparation,
                        reason="media_reply_context_open_failed",
                    )
                    return self._execution(opened, session, None, None)
                result = await preparation.fill_reply_composer(normalized)
            elif action_type == "quote" and isinstance(preparation, QuotePreparationAuthority):
                opened = await preparation.open_quote_on_target(post_url, target_post_id)
                if not opened.ok:
                    await self._abort_precommit(
                        session,
                        preparation,
                        reason="media_quote_context_open_failed",
                    )
                    return self._execution(opened, session, None, None)
                result = await preparation.fill_quote_composer(normalized)
            else:
                await self._abort_precommit(
                    session,
                    preparation,
                    reason="media_preparation_shape_mismatch",
                )
                return self._clean_failure(session, "media preparation authority shape mismatch")
        except BaseException as exc:
            await self._abort_precommit(
                session,
                preparation,
                reason="media_context_or_fill_interrupted",
            )
            return self._execution(
                hard_failure(
                    "media context/fill was interrupted before commit authority: "
                    f"{type(exc).__name__}",
                    failure_category=FailureCategory.SECURITY,
                ),
                session,
                None,
                None,
            )
        if not result.ok:
            await self._abort_precommit(
                session,
                preparation,
                reason="media_context_fill_failed",
            )
            return self._execution(result, session, None, None)
        return None

    async def _terminalize_consumed(
        self,
        session: M5ExecutionSession,
        submit_result: ActionResult,
        *,
        pre_submit_ids: set[str],
        action_type: str,
        normalized: str,
        target_post_id: str,
        actor_id: str,
        expected_count: int,
        media_digests: list[str],
    ) -> M5MediaExecution:
        exclude_ids = {target_post_id} if target_post_id else set()
        try:
            capture = await self._content_evidence.capture_new_post(
                pre_submit_ids,
                exclude_ids=exclude_ids,
            )
        except BaseException as exc:
            return self._post_submit_evidence_interrupted(
                session,
                submit_result,
                phase="capture_media_status",
                exc=exc,
                target_post_id=target_post_id or None,
            )
        capture_data = capture.data if capture.ok and isinstance(capture.data, dict) else {}
        post_id = capture_data.get("post_id")
        captured_url = capture_data.get("post_url")

        content_verification: Optional[ActionResult] = None
        try:
            if isinstance(post_id, str) and post_id and isinstance(captured_url, str) and captured_url:
                if action_type == "post":
                    content_verification = await self._content_evidence.verify_post_text(
                        captured_url,
                        normalized,
                    )
                elif action_type == "reply":
                    content_verification = await self._content_evidence.verify_reply_in_thread(
                        target_post_id=target_post_id,
                        reply_post_id=post_id,
                        actor_id=actor_id,
                        normalized_text=normalized,
                    )
                else:
                    content_verification = await self._content_evidence.verify_quote_attachment(
                        quote_post_id=post_id,
                        target_post_id=target_post_id,
                        actor_id=actor_id,
                        normalized_text=normalized,
                    )
        except BaseException as exc:
            return self._post_submit_evidence_interrupted(
                session,
                submit_result,
                phase="verify_media_content",
                exc=exc,
                target_post_id=target_post_id or None,
                post_id=post_id if isinstance(post_id, str) else None,
                post_url=captured_url if isinstance(captured_url, str) else None,
            )

        content_data = (
            content_verification.data
            if content_verification is not None
            and content_verification.ok
            and isinstance(content_verification.data, dict)
            else {}
        )
        content_ok, verified_url = self._content_proof(
            action_type=action_type,
            post_id=post_id,
            captured_url=captured_url,
            target_post_id=target_post_id,
            actor_id=actor_id,
            verification=content_verification,
            data=content_data,
        )

        media_verification: Optional[ActionResult] = None
        if content_ok and verified_url is not None:
            try:
                media_verification = await self._media_evidence.count_post_media(verified_url)
            except BaseException as exc:
                return self._post_submit_evidence_interrupted(
                    session,
                    submit_result,
                    phase="verify_media_count",
                    exc=exc,
                    target_post_id=target_post_id or None,
                    post_id=post_id if isinstance(post_id, str) else None,
                    post_url=verified_url,
                )
        media_data = (
            media_verification.data
            if media_verification is not None
            and media_verification.ok
            and isinstance(media_verification.data, dict)
            else {}
        )
        observed_count = media_data.get("media_count")
        media_count_verified = bool(
            isinstance(observed_count, int) and observed_count == expected_count
        )

        evidence = {
            "submit_result_ok": bool(submit_result.ok),
            "action_type": action_type,
            "target_post_id": target_post_id or None,
            "posted_post_id": post_id,
            "captured_url": captured_url,
            "verified_url": verified_url,
            "content_verified": content_ok,
            "media_count_expected": expected_count,
            "media_count_observed": observed_count,
            "media_count_verified": media_count_verified,
            "approved_media_sha256": list(media_digests),
            "source_byte_equivalence_verified": False,
        }

        if content_ok and media_count_verified:
            try:
                session.record_confirmed(evidence=evidence)
            except Exception as exc:  # noqa: BLE001
                return self._unresolved_persistence_failure(session, exc)
            result = ok_result(
                data={
                    "result": "media_content_posted_and_verified",
                    "action_type": action_type,
                    "posted_url": verified_url,
                    "posted_post_id": post_id,
                    "target_post_id": target_post_id or None,
                    "submitted_text": normalized,
                    "media_count_expected": expected_count,
                    "media_count_result": observed_count,
                    "media_count_verified": True,
                    "source_byte_equivalence_verified": False,
                    "m5_effect_state": AttemptState.EFFECT_CONFIRMED.value,
                }
            )
            return self._execution(
                result,
                session,
                content_verification,
                media_verification,
            )

        try:
            session.record_unknown(evidence=evidence)
        except Exception as exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, exc)
        result = hard_failure(
            "media submit crossed authority but independent content/media evidence was "
            "incomplete; reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "action_type": action_type,
            "target_post_id": target_post_id or None,
            "posted_url": verified_url or captured_url,
            "posted_post_id": post_id,
            "submitted_text": normalized,
            "media_count_expected": expected_count,
            "media_count_result": observed_count,
            "media_count_verified": media_count_verified,
            "source_byte_equivalence_verified": False,
        }
        return self._execution(
            result,
            session,
            content_verification or capture,
            media_verification,
        )

    @staticmethod
    def _content_proof(
        *,
        action_type: str,
        post_id: Any,
        captured_url: Any,
        target_post_id: str,
        actor_id: str,
        verification: Optional[ActionResult],
        data: dict[str, Any],
    ) -> tuple[bool, Optional[str]]:
        if (
            not isinstance(post_id, str)
            or not post_id.isdigit()
            or (target_post_id and post_id == target_post_id)
            or verification is None
            or not verification.ok
        ):
            return False, None

        if action_type == "post":
            if not isinstance(captured_url, str) or f"/status/{post_id}" not in captured_url:
                return False, None
            return bool(data.get("text_matches") is True), captured_url

        approved_actor = actor_id.lstrip("@").casefold()
        if action_type == "reply":
            actor = data.get("reply_actor")
            verified_url = data.get("reply_url")
            valid = bool(
                data.get("thread_bound") is True
                and data.get("text_matches") is True
                and data.get("target_post_id") == target_post_id
                and data.get("reply_post_id") == post_id
                and isinstance(actor, str)
                and actor.lstrip("@").casefold() == approved_actor
                and isinstance(verified_url, str)
                and f"/status/{post_id}" in verified_url
            )
            return valid, verified_url if valid else None

        actor = data.get("quote_actor")
        verified_url = data.get("quote_url")
        valid = bool(
            data.get("quote_attachment_verified") is True
            and data.get("text_matches") is True
            and data.get("target_post_id") == target_post_id
            and data.get("quote_post_id") == post_id
            and isinstance(actor, str)
            and actor.lstrip("@").casefold() == approved_actor
            and isinstance(verified_url, str)
            and f"/status/{post_id}" in verified_url
        )
        return valid, verified_url if valid else None

    def _post_submit_evidence_interrupted(
        self,
        session: M5ExecutionSession,
        submit_result: ActionResult,
        *,
        phase: str,
        exc: BaseException,
        target_post_id: Optional[str] = None,
        post_id: Optional[str] = None,
        post_url: Optional[str] = None,
    ) -> M5MediaExecution:
        evidence = {
            "phase": phase,
            "exception_type": type(exc).__name__,
            "submit_result_ok": bool(submit_result.ok),
            "target_post_id": target_post_id,
            "posted_post_id": post_id,
            "posted_url": post_url,
        }
        try:
            session.record_unknown(evidence=evidence)
        except Exception as persist_exc:  # noqa: BLE001
            return self._unresolved_persistence_failure(session, persist_exc)
        result = hard_failure(
            "media post-submit evidence was interrupted after authority crossed; "
            "reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
            "evidence_phase": phase,
            "target_post_id": target_post_id,
            "posted_url": post_url,
            "posted_post_id": post_id,
        }
        return self._execution(result, session, None, None)

    async def _abort_precommit(
        self,
        session: M5ExecutionSession,
        preparation: PreparationAuthority,
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
        preparation: PreparationAuthority,
        receipt: AuthorizedEffect,
        exc: BaseException,
    ) -> M5MediaExecution:
        permit = receipt.permit
        if permit is None:
            try:
                await preparation.close_composer()
            finally:
                if not session.attempt.reservation_started:
                    session.resolve_no_external_effect(
                        reason="media_submit_interrupted_before_commit_authority"
                    )
            result = hard_failure(
                "media submit interrupted before commit authority: "
                f"{type(exc).__name__}",
                failure_category=FailureCategory.UNKNOWN,
            )
            return self._execution(result, session, None, None)

        if not permit.consumed:
            try:
                await preparation.close_composer()
            finally:
                session.resolve_no_external_effect(
                    reason="media_submit_interrupted_before_permit_consumption"
                )
            result = hard_failure(
                "media submit interrupted before permit consumption: "
                f"{type(exc).__name__}",
                failure_category=FailureCategory.SECURITY,
            )
            return self._execution(result, session, None, None)

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
            "media submit was interrupted after authority crossed; reconciliation is required",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": AttemptState.EFFECT_UNKNOWN.value,
        }
        return self._execution(result, session, None, None)

    def _unresolved_persistence_failure(
        self,
        session: M5ExecutionSession,
        exc: Exception,
    ) -> M5MediaExecution:
        result = hard_failure(
            "could not persist the terminal M5 media outcome; safety state remains "
            f"unresolved ({type(exc).__name__})",
            failure_category=FailureCategory.UNKNOWN,
        )
        result.data = {
            "public_side_effect": True,
            "reconciliation_required": True,
            "m5_effect_state": session.attempt.state.value,
            "terminal_persistence_failed": True,
        }
        return self._execution(result, session, None, None)

    def _clean_failure(
        self,
        session: M5ExecutionSession,
        message: str,
    ) -> M5MediaExecution:
        return self._execution(
            hard_failure(message, failure_category=FailureCategory.SECURITY),
            session,
            None,
            None,
        )

    @staticmethod
    def _execution(
        result: ActionResult,
        session: M5ExecutionSession,
        verification: Optional[ActionResult],
        media_verification: Optional[ActionResult],
    ) -> M5MediaExecution:
        receipt = session.authorized_effect
        permit = receipt.permit if receipt is not None else None
        return M5MediaExecution(
            result=result,
            verification=verification,
            media_verification=media_verification,
            attempt_state=session.attempt.state,
            permit_issued=permit is not None,
            permit_consumed=bool(permit is not None and permit.consumed),
        )
