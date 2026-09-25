"""WriteKernel — the write-safety pipeline (Phase 0b).

Enforces: compose → preview → policy → confirm → execute → journal → verify.
Every write capability MUST pass through this kernel; it does not call the
broker directly for mutations.

Hardened design (review conversation 6a4fb320):
- Q1: confirmation tokens bind one capability + immutable intent hash (NOT bare confirm=True).
- Q2: per-action + global token bucket.
- Q3: semantic dedupe, journal-hydrated.
- Q4: multi-dimensional risk metadata, 4 derived tiers.
- Final rec #6: journal records the full pipeline trace (intent_created,
  confirmation_required, policy_decision, execute_attempted/skipped, verify).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety.dedupe import DedupeStore
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import (
    ConfirmationToken,
    PolicyDecision,
    PolicyVerdict,
    WriteIntent,
)
from webwire.safety.recovery_guard import RecoveryGuardUnavailable
from webwire.safety.risk_registry import RiskRegistry
from webwire.safety.token_bucket import TokenBucket

if TYPE_CHECKING:
    from webwire.broker import ReadOnlyBroker
    from webwire.safety.recovery_guard import RecoveryGuard

logger = logging.getLogger(__name__)

__all__ = ["WriteKernel", "WriteCapability", "PreviewResult"]

_CONFIRM_TTL_S = 300.0  # confirmation tokens expire after 5 min
_RECOVERY_GUARD_EXEMPT_CAPABILITIES = frozenset({"compose_post"})


@dataclass
class StateTransition:
    """Formal state-transition record (ChatGPT's pre-Phase-4 ask). Records the
    delta this invocation created, so compensation reverses THIS invocation's
    delta, not merely the final state."""
    pre_state: Optional[str] = None       # e.g. "not_liked", "not_bookmarked"
    intended_state: Optional[str] = None  # e.g. "liked", "bookmarked"
    post_state: Optional[str] = None      # verified actual state after execute
    changed_by_this_invocation: bool = False  # did THIS call mutate state?
    verification_result: Optional[str] = None  # "verified" | "verify_failed" | None
    compensation_eligible: bool = False   # only True if THIS invocation created the delta
    compensation_action: Optional[str] = None
    residual_side_effects: list[str] = field(default_factory=list)


@dataclass
class PreviewResult:
    """Read-only preview of what a write will do. Returned to the caller for
    human approval before execution."""
    summary: str
    target_url: Optional[str] = None
    current_state: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class WriteCapability(Protocol):
    """A write capability. The kernel drives the pipeline; the capability
    supplies the action-specific logic via these 4 methods."""

    name: str

    @property
    def tier(self) -> Any:
        """CapabilityTier; Any-typed because the enum lives in
        webwire.capabilities.base and importing it here would cycle through
        the capabilities package __init__ (which imports this module)."""
        ...

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        """Produce a declarative WriteIntent from input. NO browser interaction."""
        ...

    async def preview(self, intent: WriteIntent, broker: "ReadOnlyBroker") -> PreviewResult:
        """Read-only check of the current state. NO mutation. Receives the
        ReadOnlyBroker — preview runs BEFORE confirmation."""
        ...

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Perform the actual mutation. Receives a WriteBroker (narrow write
        surface) — only reachable after the kernel's confirmation gate."""
        ...

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Confirm the action took effect. Receives a WriteBroker."""
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
        write_broker_factory=None,  # callable(kill_switch) -> WriteBroker; set by dispatcher
        auto_approve_private: bool = False,
        recovery_guard: Optional["RecoveryGuard"] = None,
    ) -> None:
        self._kill = kill_switch
        self._risk = risk_registry
        self._bucket = token_bucket
        self._dedupe = dedupe
        self._journal = journal
        self._write_broker_factory = write_broker_factory
        self._auto_approve_private = auto_approve_private
        self._recovery_guard = recovery_guard
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
        Second call (confirmation_token present): validates the capability-bound
        token, then runs execute→journal→verify.

        Layer 6 checks durable unresolved semantic replay before any browser-
        capable preview. The only exemption is the named ``compose_post`` shell,
        whose execution contract has no remote mutation. Callers cannot disable
        the recovery gate for arbitrary capabilities.
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
        risk_tier = intent.risk_tier()
        semantic_key = intent.dedupe_key()
        trace["intent"] = {
            "action_type": intent.action_type,
            "target": f"{intent.target_type}:{intent.target_id}",
            "dedupe_key": semantic_key,
            "intent_hash": intent.intent_hash(),
            "risk_tier": risk_tier.value,
        }
        trace["stages"].append("intent_created")

        # 2b. Registry gate (P0 gap-3 fix, 2026-09-22): policy runs on REGISTRY
        # truth, not self-declared metadata. The action_type must be a known
        # registry entry, and the capability's declared risk/compensation meta
        # must match it exactly. A mismatch is a capability bug (drift between
        # its compose() and the registry) or a downgrade attempt — the honest
        # answer in both cases is a loud denial, not a silent override that
        # would mask the drift.
        reg_entry = self._risk.get(intent.action_type)
        if reg_entry is None:
            trace["stages"].append("denied:unknown_action")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY,
                reason=(
                    f"action_type {intent.action_type!r} is not registered in the "
                    "risk registry — refusing to run unregistered writes"
                ),
                risk_tier=risk_tier, intent_hash=intent.intent_hash(),
                blocked_by="unknown_action",
            ), trace, None)
        reg_meta, reg_comp = reg_entry
        if intent.risk_meta != reg_meta or intent.compensation != reg_comp:
            trace["stages"].append("denied:risk_meta_mismatch")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY,
                reason=(
                    f"declared risk/compensation meta for {intent.action_type!r} "
                    "differs from the risk registry (drift or downgrade attempt)"
                ),
                risk_tier=risk_tier, intent_hash=intent.intent_hash(),
                blocked_by="risk_meta_mismatch",
            ), trace, None)

        # 2c. Layer-6 durable recovery gate. compose() is declarative; preview()
        # may navigate. Refresh from the EffectLedger on every guarded write so
        # an EFFECT_UNKNOWN created while this process survives cannot be replayed
        # merely because startup hydration happened earlier. The exemption is an
        # internal allowlist, not a caller-controlled switch.
        recovery_exempt = write_cap.name in _RECOVERY_GUARD_EXEMPT_CAPABILITIES
        if not recovery_exempt and self._recovery_guard is not None:
            try:
                recovery_block = self._recovery_guard.require_clear(
                    semantic_key,
                    refresh=True,
                )
            except RecoveryGuardUnavailable:
                trace["stages"].append("denied:reconciliation_required")
                trace["recovery"] = {
                    "available": False,
                    "semantic_key": semantic_key,
                }
                return self._finish(
                    PolicyDecision(
                        verdict=PolicyVerdict.DENY,
                        reason=(
                            "M5 recovery authority is unavailable; reconciliation "
                            "is required before mutation"
                        ),
                        risk_tier=risk_tier,
                        intent_hash=intent.intent_hash(),
                        blocked_by="reconciliation_required",
                    ),
                    trace,
                    {
                        "reconciliation_required": True,
                        "recovery_unavailable": True,
                        "semantic_key": semantic_key,
                    },
                )
            if recovery_block is not None:
                effects = [
                    {
                        "effect_id": effect.effect_id,
                        "raw_state": effect.raw_state.value,
                        "effective_state": effect.effective_state.value,
                    }
                    for effect in recovery_block.effects
                ]
                trace["stages"].append("denied:reconciliation_required")
                trace["recovery"] = {
                    "available": True,
                    "semantic_key": semantic_key,
                    "effects": effects,
                }
                return self._finish(
                    PolicyDecision(
                        verdict=PolicyVerdict.DENY,
                        reason=(
                            "durable unresolved effect requires reconciliation before "
                            "semantic replay"
                        ),
                        risk_tier=risk_tier,
                        intent_hash=intent.intent_hash(),
                        blocked_by="reconciliation_required",
                    ),
                    trace,
                    {
                        "reconciliation_required": True,
                        "semantic_key": semantic_key,
                        "effects": effects,
                    },
                )

        # 3. Policy evaluation (before preview — cheap gates first).

        # 3a. Token bucket.
        allowed, reason = self._bucket.acquire(intent.action_type, risk_tier)
        if not allowed:
            trace["stages"].append("denied:token_bucket")
            return self._finish(PolicyDecision(
                verdict=PolicyVerdict.DENY, reason=reason, risk_tier=risk_tier,
                intent_hash=intent.intent_hash(), blocked_by="token_bucket",
            ), trace, None)

        # 3b. Dedupe.
        if not self._dedupe.check(semantic_key):
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

        # 5. Confirmation gate (capability + intent bound).
        provided_token = input.get("confirmation_token")
        if provided_token is None:
            # First phase: issue a token and return confirmation_required.
            token = self._issue_token(intent, write_cap.name)
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
                "capability_name": token.capability_name,
                "expires_at": token.expires_at,
            })

        # Second phase: validate the token.
        pending = self._pending_tokens.get(provided_token)
        decision = self._validate_token(
            pending,
            intent,
            input,
            capability_name=write_cap.name,
        )
        if decision is not None:
            trace["stages"].append(f"denied:{decision.blocked_by}")
            return self._finish(decision, trace, None)

        # Token valid — consume it.
        assert pending is not None
        pending.consumed = True
        trace["stages"].append("confirmed")

        # 6. Execute. Construct a WriteBroker (narrow write surface) for the
        # execute + verify stages. Preview used the ReadOnlyBroker; execute
        # uses the WriteBroker — only reachable after confirmation.
        write_broker = self._write_broker_factory() if self._write_broker_factory else broker
        trace["stages"].append("execute_attempted")
        exec_result = await write_cap.execute(intent, write_broker)
        trace["execute_ok"] = exec_result.ok

        exec_data = exec_result.data if isinstance(exec_result.data, dict) else {}
        side_effect_free = exec_data.get("dry_run") is True
        if (exec_result.ok or exec_data.get("public_side_effect") is True) and not side_effect_free:
            # Record in dedupe (so a retry is blocked within TTL). Rule (P0
            # hydration spec, decision 1): a clean success records, AND an
            # uncertain submit (degraded result flagged public_side_effect,
            # e.g. submit_clicked_verification_pending) records too — for
            # irreversible writes, "we don't know" is treated as "it happened".
            # EXCEPT: an execute that declares itself side-effect-free
            # (dry_run=True, e.g. compose_post's deliberate no-op) records
            # nothing — dedupe guards side effects, and a no-op has none
            # (otherwise a confirmed dry-run would block the real post).
            self._dedupe.record(semantic_key)
            trace["dedupe_recorded"] = True
        else:
            trace["dedupe_recorded"] = False

        # 7. Journal — record the full pipeline trace.
        trace["stages"].append("journalled")

        # 8. Verify — only after a successful execute. Running verify after a
        # failed execute wastes browser work and produced misleading traces
        # (verify_ok=True beside execute_ok=False, because "unknown" state
        # reads counted as pass). Failed execute → verify skipped, honestly.
        if exec_result.ok:
            verify_result = await write_cap.verify(intent, write_broker)
            trace["verify_ok"] = verify_result.ok
            trace["stages"].append("verified" if verify_result.ok else "verify_failed")
        else:
            trace["verify_ok"] = False
            trace["stages"].append("verify_skipped_execute_failed")

        return self._finish(PolicyDecision(
            verdict=PolicyVerdict.ALLOW if exec_result.ok else PolicyVerdict.DENY,
            reason="executed" if exec_result.ok else "execute_failed",
            risk_tier=risk_tier, intent_hash=intent.intent_hash(),
        ), trace, exec_result.data)

    # -- internals -----------------------------------------------------------

    def _issue_token(
        self,
        intent: WriteIntent,
        capability_name: str,
    ) -> ConfirmationToken:
        """Issue a token bound to one capability and one immutable intent."""
        import secrets

        token_str = secrets.token_urlsafe(16)
        now = time.time()
        token = ConfirmationToken(
            token=token_str,
            intent_hash=intent.intent_hash(),
            risk_tier=intent.risk_tier(),
            capability_name=capability_name,
            created_at=now,
            expires_at=now + _CONFIRM_TTL_S,
        )
        self._pending_tokens[token_str] = token
        return token

    def _validate_token(
        self,
        token: Optional[ConfirmationToken],
        intent: WriteIntent,
        input: dict[str, Any],
        *,
        capability_name: str,
    ) -> Optional[PolicyDecision]:
        """Return a DENY decision for invalid confirmation authority, else None."""
        del input
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
        if token.capability_name != capability_name:
            return PolicyDecision(
                verdict=PolicyVerdict.DENY,
                reason=(
                    "capability changed since confirmation "
                    f"({token.capability_name!r} != {capability_name!r})"
                ),
                risk_tier=intent.risk_tier(), intent_hash=intent.intent_hash(),
                blocked_by="capability_mismatch",
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
        from super_browser.results import ActionError, ErrorCategory, action_result
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
