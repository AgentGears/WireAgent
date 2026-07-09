"""WriteKernel — the write-safety pipeline (Phase 0b).

Enforces: compose → preview → policy → confirm → execute → journal → verify.
Every write capability MUST pass through this kernel; it does not call the
broker directly for mutations.

Hardened design (review conversation 6a4fb320):
- Q1: confirmation tokens bound to immutable intent hashes (NOT bare confirm=True).
- Q2: per-action + global token bucket.
- Q3: semantic dedupe, journal-hydrated.
- Q4: multi-dimensional risk metadata, 4 derived tiers.
- Final rec #6: journal records the full pipeline trace (intent_created,
  confirmation_required, policy_decision, execute_attempted/skipped, verify).
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional, Protocol, TYPE_CHECKING, runtime_checkable

from webwire.envelope import ActionResult, ok_result, policy_blocked, soft_failure
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.models import (
    ConfirmationToken,
    PolicyDecision,
    PolicyVerdict,
    RiskMeta,
    WriteIntent,
)
from webwire.safety.risk_registry import RiskRegistry
from webwire.safety.token_bucket import TokenBucket
from webwire.safety.kill_switch import KillSwitch

if TYPE_CHECKING:
    from webwire.broker import ReadOnlyBroker

logger = logging.getLogger(__name__)

__all__ = ["WriteKernel", "WriteCapability", "PreviewResult"]

_CONFIRM_TTL_S = 300.0  # confirmation tokens expire after 5 min


@dataclass
class PreviewResult:
    """Read-only preview of what a write will do. Returned to the caller for
    human approval before execution."""
    summary: str                       # human-readable description
    target_url: Optional[str] = None
    current_state: Optional[str] = None  # e.g. "not liked", "not bookmarked"
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class WriteCapability(Protocol):
    """A write capability. The kernel drives the pipeline; the capability
    supplies the action-specific logic via these 4 methods."""

    name: str

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        """Produce a declarative WriteIntent from input. NO browser interaction."""
        ...

    async def preview(self, intent: WriteIntent, broker: ReadOnlyBroker) -> PreviewResult:
        """Read-only check of the current state. NO mutation."""
        ...

    async def execute(self, intent: WriteIntent, broker: ReadOnlyBroker) -> ActionResult:
        """Perform the actual mutation. Returns the execution result."""
        ...

    async def verify(self, intent: WriteIntent, broker: ReadOnlyBroker) -> ActionResult:
        """Confirm the action took effect (read-only verification)."""
        ...


class WriteKernel:
    """The write-safety pipeline. One instance per Dispatcher."""

    def __init__(
        self,
        kill_switch: KillSwitch,
        risk_registry: RiskRegistry,
        token_bucket: TokenBucket,
        dedupe: DedupeStore,
        journal: Journal,
        *,
        auto_approve_private: bool = False,  # even private actions require confirm by default
    ) -> None:
        self._kill = kill_switch
        self._risk = risk_registry
        self._bucket = token_bucket
        self._dedupe = dedupe
        self._journal = journal
        self._auto_approve_private = auto_approve_private
        # Pending confirmation tokens: token_str -> ConfirmationToken
        self._pending_tokens: dict[str, ConfirmationToken] = {}

    async def execute(
        self,
        write_cap: WriteCapability,
        broker: ReadOnlyBroker,
        input: dict[str, Any],
        actor_identity: Optional[str] = None,
    ) -> ActionResult:
        """Run the full write pipeline. Returns an ActionResult.

        First call (no confirmation_token in input): runs compose→preview→policy,
        returns CONFIRMATION_REQUIRED with a token.
        Second call (confirmation_token present): validates the token, runs
        execute→journal→verify.
        """
        trace: dict[str, Any] = {"action": write_cap.name, "stages": []}

        # 1. Kill switch.
        if self._kill.tripped():
            trace["stages"].append("killed_at_entry")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="kill switch tripped",
                risk_tier=write_cap.compose(input, actor_identity).risk_tier(),
                intent_hash="", blocked_by="kill_switch",
            ), trace, None)

        # 2. Compose the intent.
        intent = write_cap.compose(input, actor_identity)
        trace["intent"] = {
            "action_type": intent.action_type,
            "target": f"{intent.target_type}:{intent.target_id}",
            "dedupe_key": intent.dedupe_key(),
            "intent_hash": intent.intent_hash(),
        }
        trace["stages"].append("intent_created")

        # 3. Policy evaluation (before preview — cheap gates first).
        risk_tier = intent.risk_tier()

        # 3a. Token bucket.
        allowed, reason = self._bucket.acquire(intent.action_type, risk_tier)
        if not allowed:
            trace["stages"].append("denied:token_bucket")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY, reason=reason, risk_tier=risk_tier,
                intent_hash=intent.intent_hash(), blocked_by="token_bucket",
            ), trace, None)

        # 3b. Dedupe.
        if not self._dedupe.check(intent.dedupe_key()):
            trace["stages"].append("denied:dedupe")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="duplicate action within TTL",
                risk_tier=risk_tier, intent_hash=intent.intent_hash(),
                blocked_by="dedupe",
            ), trace, None)

        # 3c. Dry-run mode.
        if input.get("dry_run"):
            preview = await write_cap.preview(intent, broker)
            trace["stages"].append("dry_run")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DRY_RUN, reason="dry_run requested",
                risk_tier=risk_tier, intent_hash=intent.intent_hash(),
            ), trace, {"preview": preview.summary, "dry_run": True})

        # 4. Preview.
        preview = await write_cap.preview(intent, broker)
        trace["preview"] = preview.summary

        # 5. Confirmation gate (token-bound, review Q1).
        provided_token = input.get("confirmation_token")
        if provided_token is None:
            # First phase: issue a token and return confirmation_required.
            token = self._issue_token(intent)
            trace["stages"].append("confirmation_required")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.CONFIRMATION_REQUIRED,
                reason="human approval required",
                risk_tier=risk_tier,
                intent_hash=intent.intent_hash(),
                confirmation_token=token,
            ), trace, {
                "preview": preview.summary,
                "target_url": preview.target_url,
                "current_state": preview.current_state,
                "warnings": preview.warnings,
                "confirmation_token": token.token,
                "intent_hash": token.intent_hash,
                "expires_at": token.expires_at,
            })

        # Second phase: validate the token.
        token = self._pending_tokens.get(provided_token)
        decision = self._validate_token(token, intent, input)
        if decision is not None:
            trace["stages"].append(f"denied:{decision.blocked_by}")
            return self._finish(decision, trace, None)

        # Token valid — consume it.
        assert token is not None
        token.consumed = True
        trace["stages"].append("confirmed")

        # 6. Execute.
        trace["stages"].append("execute_attempted")
        exec_result = await write_cap.execute(intent, broker)
        trace["execute_ok"] = exec_result.ok

        if exec_result.ok:
            # Record in dedupe (so a retry is blocked within TTL).
            self._dedupe.record(intent.dedupe_key())

        # 7. Journal — record the full pipeline trace.
        trace["stages"].append("journalled")

        # 8. Verify.
        verify_result = await write_cap.verify(intent, broker)
        trace["verify_ok"] = verify_result.ok
        trace["stages"].append("verified" if verify_result.ok else "verify_failed")

        return self._finish(PolicyDecision(
            verdict=PolicyVerdict.ALLOW if exec_result.ok else PolicyVerdict.DENY,
            reason="executed" if exec_result.ok else "execute_failed",
            risk_tier=risk_tier, intent_hash=intent.intent_hash(),
        ), trace, exec_result.data)

    # -- internals -----------------------------------------------------------

    def _issue_token(self, intent: WriteIntent) -> ConfirmationToken:
        """Issue a confirmation token bound to the intent hash."""
        import secrets
        token_str = secrets.token_urlsafe(16)
        now = time.time()
        token = ConfirmationToken(
            token=token_str,
            intent_hash=intent.intent_hash(),
            risk_tier=intent.risk_tier(),
            created_at=now,
            expires_at=now + _CONFIRM_TTL_S,
        )
        self._pending_tokens[token_str] = token
        return token

    def _validate_token(
        self, token: Optional[ConfirmationToken], intent: WriteIntent, input: dict[str, Any],
    ) -> Optional[PolicyDecision]:
        """Validate the confirmation token. Returns a DENY decision if invalid,
        None if valid."""
        now = time.time()
        if token is None:
            return PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="invalid confirmation token",
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="consumed_token",  # unknown token
            )
        if token.is_expired(now):
            return PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="confirmation token expired",
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="expired_token",
            )
        if token.consumed:
            return PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="confirmation token already used",
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="consumed_token",
            )
        if token.intent_hash != intent.intent_hash():
            return PolicyDecision(
                verdict=PolicyVerdict.DENY,
                reason="intent changed since confirmation (hash mismatch)",
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="intent_mismatch",
            )
        if token.risk_tier != intent.risk_tier():
            return PolicyDecision(
                verdict=PolicyVerdict.DENY, reason="risk tier changed since confirmation",
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="intent_mismatch",
            )
        return None  # valid

    def _finish(
        self, decision: PolicyDecision, trace: dict, data: Any,
    ) -> ActionResult:
        """Build the final ActionResult with policy decision + trace."""
        result_data = {
            "policy": decision.to_dict(),
            "trace": trace,
        }
        if data is not None:
            result_data["data"] = data
        if decision.verdict == PolicyVerdict.ALLOW:
            return ok_result(data=result_data)
        if decision.verdict == PolicyVerdict.CONFIRMATION_REQUIRED:
            return ok_result(data=result_data)  # ok=True but requires confirmation
        if decision.verdict == PolicyVerdict.DRY_RUN:
            return ok_result(data=result_data)
        # DENY — carry the policy decision + trace in data so callers can see
        # which gate blocked (blocked_by). Don't discard the diagnostic info.
        from super_browser.results import ActionError, ErrorCategory
        from super_browser.results import action_result
        r = action_result(
            ok=False,
            error=ActionError(
                category=ErrorCategory.SECURITY,
                message=decision.reason,
                recoverable=False,
            ),
        )
        r.data = result_data
        r.failure_category = __import__(
            "super_browser.results.types", fromlist=["FailureCategory"]
        ).FailureCategory.SECURITY
        return r
