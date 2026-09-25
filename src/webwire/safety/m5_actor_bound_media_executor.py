"""Actor-bound media-post terminalization for the supported live M5 path."""

from __future__ import annotations

from typing import Any, Optional

from webwire.envelope import ActionResult
from webwire.safety.m5_actor_bound_evidence import status_url_identity
from webwire.safety.m5_media_executor import M5MediaExecutor

__all__ = ["M5ActorBoundMediaExecutor"]


class M5ActorBoundMediaExecutor(M5MediaExecutor):
    """Tighten plain-media-post confirmation to the immutable approved actor.

    Reply and quote media already prove actor/target lineage explicitly in their
    evidence contracts.  Plain media posts need the same actor binding on top of
    direct status/text ownership returned by ``M5ActorBoundEvidenceReader``.
    """

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
        if action_type != "post":
            return M5MediaExecutor._content_proof(
                action_type=action_type,
                post_id=post_id,
                captured_url=captured_url,
                target_post_id=target_post_id,
                actor_id=actor_id,
                verification=verification,
                data=data,
            )

        if (
            not isinstance(post_id, str)
            or not post_id.isdigit()
            or not isinstance(captured_url, str)
            or verification is None
            or not verification.ok
        ):
            return False, None

        approved_actor = actor_id.lstrip("@").casefold()
        observed_actor = data.get("post_actor")
        observed_id = data.get("post_id")
        verified_url = data.get("post_url")
        captured_identity = status_url_identity(captured_url)
        verified_identity = (
            status_url_identity(verified_url) if isinstance(verified_url, str) else None
        )
        valid = bool(
            approved_actor
            and data.get("text_matches") is True
            and data.get("direct_status_owned") is True
            and observed_id == post_id
            and isinstance(observed_actor, str)
            and observed_actor.lstrip("@").casefold() == approved_actor
            and captured_identity == (approved_actor, post_id)
            and verified_identity == (approved_actor, post_id)
        )
        return valid, verified_url if valid and isinstance(verified_url, str) else None
