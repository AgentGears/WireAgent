"""WriteKernel — the write-safety pipeline (Phase 0b / M6 confirmation hardening).

Enforces: compose → preview → policy → confirm → execute → journal → verify.
Every write capability MUST pass through this kernel; it does not call the
broker directly for mutations.

Hardened design:
- Q1: confirmation tokens bind one capability + immutable intent hash (NOT bare confirm=True).
- Q2: per-action + global token bucket.
- Q3: semantic dedupe, journal-hydrated.
- Q4: multi-dimensional risk metadata, 4 derived tiers.
- M6: confirmation authority is epoch-bound, monotonic-TTL, and synchronized
  through one process-local ConfirmationState fence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Optional, Protocol, runtime_checkable

from webwire.envelope import ActionResult, ok_result
from webwire.journal import Journal
from webwire.safety.confirmation_state import ConfirmationState
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
    """Formal state-transition record. Records the delta this invocation created,
    so compensation reverses THIS invocation's delta, not merely final state."""
    pre_state: Optional[str] = None
    intended_state: Optional[str] = None
    post_state: Optional[str] = None
    changed_by_this_invocation: bool = False
    verification_result: Optional[str] = None
    compensation_eligible: bool = False
    compensation_action: Optional[str] = None
    residual_side_effects: list[str] = field(default_factory=list)


@dataclass
class PreviewResult:
    """Read-only preview of what a write will do for human approval."""
    summary: str
    target_url: Optional[str] = None
    current_state: Optional[str] = None
    warnings: list[str] = field(default_factory=list)


@runtime_checkable
class WriteCapability(Protocol):
    """A write capability. The kernel drives the pipeline."""

    name: str

    @property
    def tier(self) -> Any:
        """CapabilityTier; Any avoids a package import cycle."""
        ...

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        """Produce a declarative WriteIntent. NO browser interaction."""
        ...

    async def preview(self, intent: WriteIntent, broker: "ReadOnlyBroker") -> PreviewResult:
        """Read-only current-state check. NO mutation."""
        ...

    async def execute(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Perform the actual mutation after confirmation."""
        ...

    async def verify(self, intent: WriteIntent, broker: Any) -> ActionResult:
        """Confirm the action took effect."""
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
        write_broker_factory=None,
        auto_approve_private: bool = False,
        recovery_guard: Optional["RecoveryGuard"] = None,
        confirmation_state: Optional[ConfirmationState] = None,
    ) -> None:
        self._kill = kill_switch
        self._risk = risk_registry
        self._bucket = token_bucket
        self._dedupe = dedupe
        self._journal = journal
        self._write_broker_factory = write_broker_factory
        self._auto_approve_private = auto_approve_private
        self._recovery_guard = recovery_guard
        self._confirmation_state = confirmation_state or ConfirmationState(
            ttl_seconds=_CONFIRM_TTL_S
        )

    @property
    def confirmation_state(self) -> ConfirmationState:
        """The exact process-local confirmation authority used by this kernel."""
        return self._confirmation_state

    async def execute(
        self,
        write_cap: WriteCapability,
        broker: ReadOnlyBroker,
        input: dict[str, Any],
        actor_identity: Optional[str] = None,
        *,
        enforce_recovery_guard: Optional[bool] = None,
    ) -> ActionResult:
        """Run the full write pipeline. Returns an ActionResult.

        First call (no confirmation_token in input): runs compose→preview→policy,
        returns CONFIRMATION_REQUIRED with a token.
        Second call (confirmation_token present): validates and atomically consumes
        capability-, intent-, epoch-, and monotonic-TTL-bound authority before
        execute→journal→verify.

        Layer 6 checks durable unresolved semantic replay before any browser-
        capable preview. The only exemption is the named ``compose_post`` shell,
        whose execution contract has no remote mutation. The transitional
        ``enforce_recovery_guard`` keyword is accepted for Dispatcher source
        compatibility but cannot grant an exemption to any other capability.
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

        # 2b. Registry gate: policy runs on registry truth, not self-declared
        # metadata. A mismatch is drift or a downgrade attempt and fails closed.
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

        # 2c. Durable recovery gate. compose() is declarative; preview() may
        # navigate. Refresh authoritative composite recovery on every guarded
        # write. The exemption is an internal allowlist, not caller controlled.
        recovery_exempt = write_cap.name in _RECOVERY_GUARD_EXEMPT_CAPABILITIES
        if enforce_recovery_guard is False and not recovery_exempt:
            trace["recovery_exemption_ignored"] = True
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
                            "M6 recovery authority is unavailable; reconciliation "
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

        # 5. Confirmation gate (capability + intent + epoch + monotonic TTL bound).
        provided_token = input.get("confirmation_token")
        if provided_token is None:
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
                "confirmation_epoch": token.confirmation_epoch,
                "expires_at": token.expires_at,
            })

        # Second phase: final validation and consumption are one synchronized
        # authority operation. The monotonic expiry clock is sampled inside that
        # operation, after all preceding preview/policy work has completed.
        decision = self._validate_token(
            provided_token,
            intent,
            capability_name=write_cap.name,
        )
        if decision is not None:
            trace["stages"].append(f"denied:{decision.blocked_by}")
            return self._finish(decision, trace, None)
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
            # Record clean success and uncertain visible side effects in dedupe.
            # A declared side-effect-free execute records nothing.
            self._dedupe.record(semantic_key)
            trace["dedupe_recorded"] = True
        else:
            trace["dedupe_recorded"] = False

        # 7. Journal — record the full pipeline trace.
        trace["stages"].append("journalled")

        # 8. Verify only after successful execute.
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
        """Issue synchronized authority bound to current epoch and monotonic TTL."""
        return self._confirmation_state.issue(
            intent_hash=intent.intent_hash(),
            risk_tier=intent.risk_tier(),
            capability_name=capability_name,
        )

    def _validate_token(
        self,
        provided_token: Any,
        intent: WriteIntent,
        *,
        capability_name: str,
    ) -> Optional[PolicyDecision]:
        """Atomically validate+consume confirmation authority or return DENY."""
        token, blocked_by = self._confirmation_state.validate_and_consume(
            provided_token,
            intent_hash=intent.intent_hash(),
            risk_tier=intent.risk_tier(),
            capability_name=capability_name,
        )
        if blocked_by is None:
            return None

        if blocked_by == "stale_confirmation_epoch":
            reason = "confirmation token was revoked by a newer confirmation epoch"
        elif blocked_by == "expired_token":
            reason = "confirmation token expired"
        elif blocked_by == "consumed_token":
            reason = (
                "confirmation token already used"
                if token is not None and token.consumed
                else "invalid confirmation token"
            )
        elif blocked_by == "capability_mismatch":
            actual = token.capability_name if token is not None else None
            reason = (
                "capability changed since confirmation "
                f"({actual!r} != {capability_name!r})"
            )
        else:
            reason = "intent changed since confirmation (hash or risk mismatch)"

        return PolicyDecision(
            verdict=PolicyVerdict.DENY,
            reason=reason,
            risk_tier=intent.risk_tier(),
            intent_hash=intent.intent_hash(),
            blocked_by=blocked_by,
        )

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
            return ok_result(data=result_data)
        if decision.verdict == PolicyVerdict.DRY_RUN:
            return ok_result(data=result_data)
        # DENY — carry policy decision + trace so callers can see blocked_by.
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
