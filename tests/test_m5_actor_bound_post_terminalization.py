"""Terminal-decision regressions for actor-bound plain and media posts."""

from __future__ import annotations

from dataclasses import dataclass

from webwire.envelope import ok_result
from webwire.safety.execution_models import AttemptState
from webwire.safety.m5_actor_bound_media_executor import M5ActorBoundMediaExecutor
from webwire.safety.m5_actor_bound_post_executor import M5ActorBoundPostTextExecutor


@dataclass
class _Permit:
    actor_id: str = "@actor"
    consumed: bool = True


@dataclass
class _Receipt:
    permit: _Permit


@dataclass
class _Attempt:
    state: AttemptState = AttemptState.RESERVED


class _Session:
    def __init__(self, receipt: _Receipt) -> None:
        self.authorized_effect = receipt
        self.attempt = _Attempt()
        self.recorded_evidence = None

    def record_confirmed(self, *, evidence=None) -> None:  # type: ignore[no-untyped-def]
        self.recorded_evidence = evidence
        self.attempt.state = AttemptState.EFFECT_CONFIRMED

    def record_unknown(self, *, evidence=None) -> None:  # type: ignore[no-untyped-def]
        self.recorded_evidence = evidence
        self.attempt.state = AttemptState.EFFECT_UNKNOWN


class _Evidence:
    def __init__(
        self,
        *,
        captured_actor: str = "actor",
        observed_actor: str = "actor",
    ) -> None:
        self.captured_actor = captured_actor
        self.observed_actor = observed_actor

    async def capture_new_post(self, pre_submit_ids, *, exclude_ids=None):  # type: ignore[no-untyped-def]
        return ok_result(
            data={
                "post_id": "99",
                "post_url": f"https://x.com/{self.captured_actor}/status/99",
            }
        )

    async def verify_post_text(self, post_url: str, normalized_text: str):  # type: ignore[no-untyped-def]
        return ok_result(
            data={
                "text_matches": True,
                "direct_status_owned": True,
                "post_id": "99",
                "post_actor": self.observed_actor,
                "post_url": f"https://x.com/{self.observed_actor}/status/99",
            }
        )


def _post_executor(evidence: _Evidence) -> M5ActorBoundPostTextExecutor:
    return M5ActorBoundPostTextExecutor(  # type: ignore[arg-type]
        runtime=object(),
        evidence_reader=evidence,
    )


async def test_plain_post_confirms_only_when_actor_id_and_urls_all_bind() -> None:
    receipt = _Receipt(_Permit())
    session = _Session(receipt)
    executor = _post_executor(_Evidence())

    execution = await executor._terminalize_consumed(  # type: ignore[arg-type]
        session,
        receipt,
        ok_result(data={"submitted": True}),
        {"1", "2"},
        "approved text",
    )

    assert execution.result.ok is True
    assert execution.attempt_state is AttemptState.EFFECT_CONFIRMED
    assert session.recorded_evidence["actor_verified"] is True
    assert session.recorded_evidence["status_identity_verified"] is True


async def test_plain_post_same_text_wrong_observed_actor_is_unknown() -> None:
    receipt = _Receipt(_Permit())
    session = _Session(receipt)
    executor = _post_executor(_Evidence(observed_actor="other"))

    execution = await executor._terminalize_consumed(  # type: ignore[arg-type]
        session,
        receipt,
        ok_result(data={"submitted": True}),
        set(),
        "approved text",
    )

    assert execution.result.ok is False
    assert execution.result.failure_category.value == "unknown"
    assert execution.result.data["reconciliation_required"] is True
    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert session.recorded_evidence["actor_verified"] is False


async def test_plain_post_wrong_actor_capture_url_is_unknown_even_if_dom_actor_matches() -> None:
    receipt = _Receipt(_Permit())
    session = _Session(receipt)
    executor = _post_executor(_Evidence(captured_actor="other", observed_actor="actor"))

    execution = await executor._terminalize_consumed(  # type: ignore[arg-type]
        session,
        receipt,
        ok_result(data={"submitted": True}),
        set(),
        "approved text",
    )

    assert execution.attempt_state is AttemptState.EFFECT_UNKNOWN
    assert session.recorded_evidence["actor_verified"] is True
    assert session.recorded_evidence["status_identity_verified"] is False


def _media_verification(actor: str = "actor"):
    return ok_result(
        data={
            "text_matches": True,
            "direct_status_owned": True,
            "post_id": "99",
            "post_actor": actor,
            "post_url": f"https://x.com/{actor}/status/99",
        }
    )


def test_media_post_content_proof_requires_approved_actor_binding() -> None:
    verified, url = M5ActorBoundMediaExecutor._content_proof(
        action_type="post",
        post_id="99",
        captured_url="https://x.com/actor/status/99",
        target_post_id="",
        actor_id="@actor",
        verification=_media_verification(),
        data=_media_verification().data,
    )

    assert verified is True
    assert url == "https://x.com/actor/status/99"


def test_media_post_wrong_actor_cannot_confirm_same_text_status() -> None:
    verification = _media_verification("other")
    verified, url = M5ActorBoundMediaExecutor._content_proof(
        action_type="post",
        post_id="99",
        captured_url="https://x.com/other/status/99",
        target_post_id="",
        actor_id="@actor",
        verification=verification,
        data=verification.data,
    )

    assert verified is False
    assert url is None
