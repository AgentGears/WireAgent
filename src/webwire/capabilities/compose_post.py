"""compose_post capability — Phase 4a dry-run compose (NO submit).

Per ChatGPT's Phase 4a directive: prove the irreversible-write safety contract
before any public post is possible. This capability exercises:
  compose (normalize text) → preview → policy (token-bound) → confirm → dry-run
but the execute stage is a NO-OP — no browser submit, no public side effect.

The 7 Phase 4 invariants are enforced:
1. normalized_text = canonicalize(input); token binds normalized_text.
2. Token binds capability + normalized_text + account + dedupe_key.
3. Execute receives the frozen intent, not raw text.
4. Kill switch checked (by the kernel, before execute).
5. Journal records the compose intent (no posted URL yet — dry-run).
6. Dedupe key = actor + "post" + text_hash(normalized_text).
7. Policy classifies as PUBLIC_CONTENT_IRREVERSIBLE.

Phase 4b will add the actual submit path to a separate post_text capability.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult, ok_result, soft_failure
from webwire.safety import DEFAULT_REGISTRY, WriteIntent
from webwire.safety.text_normalize import normalize_text, text_hash, validate_length
from webwire.safety.write_kernel import PreviewResult

logger = logging.getLogger(__name__)

__all__ = ["ComposePostCapability"]


class ComposePostCapability:
    """Dry-run compose for a public post. NO submit — Phase 4a only."""

    name = "compose_post"

    @property
    def tier(self):  # type: ignore[no-untyped-def]
        from webwire.capabilities.base import CapabilityTier
        return CapabilityTier.WRITE

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        """Produce a WriteIntent with normalized text bound to the hash + dedupe key."""
        raw_text = input.get("text", "")
        normalized = normalize_text(raw_text)

        meta, comp = DEFAULT_REGISTRY.get("post")
        return WriteIntent(
            action_type="post",
            target_type="none",  # posts don't target a specific post
            target_id="none",
            risk_meta=meta,
            compensation=comp,
            # Dedupe key includes text hash (invariant #6).
            semantic_variant=text_hash(normalized),
            actor_identity=actor_identity,
            payload={
                "normalized_text": normalized,
                "char_count": len(normalized),
            },
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        """Preview the exact text that WILL be posted. The text shown here MUST
        equal the text submitted (invariant #1)."""
        normalized = intent.payload.get("normalized_text", "")
        char_count = intent.payload.get("char_count", len(normalized))
        is_valid, _ = validate_length(normalized)

        warnings = []
        if not is_valid:
            warnings.append(f"Text exceeds X's character limit ({char_count} > 280)")
        if not normalized:
            warnings.append("Empty post text")

        return PreviewResult(
            summary=f"Will post: {normalized[:100]!r}",
            current_state=f"normalized_text ({char_count} chars)",
            warnings=warnings,
        )

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Phase 4a: NO-OP. No browser submit. This is dry-run only.

        Phase 4b will replace this with the actual submit path in a separate
        post_text capability. For now, execute 'succeeds' without any side effect.
        """
        normalized = intent.payload.get("normalized_text", "")

        # Validate one more time (invariant: no mutation between preview and execute).
        is_valid, char_count = validate_length(normalized)
        if not is_valid:
            return soft_failure(
                f"Text exceeds X's character limit ({char_count} > 280). "
                "Policy rejected — no post submitted.",
                failure_category=FailureCategory.VALIDATION,
            )

        return ok_result(data={
            "dry_run": True,
            "normalized_text": normalized,
            "char_count": char_count,
            "note": "Phase 4a dry-run: no submit path exists. "
                    "Phase 4b will add post_text with live execution.",
            "write_tier": "public_content_irreversible",
            "supports_compensation": False,
        })

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Phase 4a: verify that the dry-run produced the expected output
        (no posted URL — nothing was submitted)."""
        return ok_result(data={
            "verified": True,
            "note": "Dry-run verification: no public content was submitted.",
        })
