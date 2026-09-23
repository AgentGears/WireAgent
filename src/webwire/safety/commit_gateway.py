"""M5 layer 3 — Commit Gateway and single-use EffectPermit.

This module composes the frozen layer-1 policy/ledger primitives with the
layer-2 ApprovalGrant/EffectAttempt lifecycle.  It deliberately does NOT expose
scoped broker authority objects; those land in layer 4.  The gateway establishes
the transaction boundary and produces a permit that layer 4 will consume at the
actual broker mutation boundary.

Protocol:
  claimed ApprovalGrant + PREPARING EffectAttempt
      -> validate current EffectPolicy and all grant bindings
      -> final kill-switch check
      -> REQUIRED: append durable RESERVED + fsync
      -> mark RESERVED (fenced only)
      -> spend ApprovalGrant
      -> mint bound, expiring, single-use EffectPermit
      -> consume_permit() immediately before the mutation
      -> record_effect_confirmed() OR record_effect_unknown()

A crash after a fenced reservation and before a terminal record leaves raw
RESERVED evidence; EffectLedger recovery projects it as EFFECT_UNKNOWN.  No
exactly-once claim is made.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
)
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
)
from webwire.safety.execution_models import (
    ApprovalGrant,
    AttemptState,
    AuthorizationEpoch,
    EffectAttempt,
    GrantState,
)
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import WriteIntent

__all__ = [
    "DEFAULT_PERMIT_TTL_S",
    "CommitGateway",
    "EffectPermit",
    "GatewayDenied",
    "GatewayStateError",
]

DEFAULT_PERMIT_TTL_S = 30.0


class GatewayDenied(RuntimeError):
    """Commit authority was denied before the requested mutation boundary."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"gateway denied: {reason}" + (f" — {detail}" if detail else ""))


class GatewayStateError(RuntimeError):
    """Gateway/outcome API was called with an inconsistent lifecycle object."""


@dataclass
class EffectPermit:
    """Single-use authority descended from one human ApprovalGrant.

    The permit is process-local and ephemeral.  For a fenced effect the durable
    authority fact is the RESERVED ledger record; the permit merely carries the
    exact bindings layer 4 must re-check at the mutation boundary.
    """

    grant_id: str
    attempt_id: str
    effect_id: str
    semantic_key: str
    intent_hash: str
    actor_id: str
    action_type: str
    target_type: str
    target_id: str
    policy_binding: str
    authorization_epoch: int
    allowed_effects: frozenset[EffectVerb]
    fenced: bool
    issued_at: float
    expires_at: float
    permit_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    consumed: bool = False
    consumed_effect: Optional[EffectVerb] = None
    consumed_at: Optional[float] = None


class CommitGateway:
    """The one M5 commit-authority boundary.

    Layer 3 owns authorization, durable reservation, spend ordering, permit
    validation/consumption, and terminal effect-knowledge recording.  It does
    not own browser methods; layer 4 will adapt a consumed permit to a scoped
    authority surface.
    """

    def __init__(
        self,
        *,
        ledger: EffectLedger,
        kill_switch: KillSwitch,
        authorization_epoch: AuthorizationEpoch,
        policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
        clock: Callable[[], float] = time.time,
        permit_ttl_seconds: float = DEFAULT_PERMIT_TTL_S,
    ) -> None:
        self._ledger = ledger
        self._kill = kill_switch
        self._epoch = authorization_epoch
        self._policies = policies
        self._clock = clock
        self._permit_ttl = permit_ttl_seconds

    @property
    def authorization_epoch(self) -> int:
        return self._epoch.current

    def _policy_for(self, intent: WriteIntent) -> EffectPolicy:
        try:
            policy = self._policies.require(intent.action_type)
        except KeyError as exc:
            raise GatewayDenied("policy_missing", str(exc)) from exc
        policy.validate()
        return policy

    def _check_kill(self) -> None:
        if self._kill.tripped():
            raise GatewayDenied("kill_switch")

    @staticmethod
    def _validate_grant_identity(
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
        policy_binding: str,
        authorization_epoch: int,
    ) -> None:
        if attempt.grant_id != grant.grant_id:
            raise GatewayDenied("grant_mismatch")
        if grant.claimed_by != attempt.attempt_id:
            raise GatewayDenied("claim_not_held")
        if attempt.state is not AttemptState.PREPARING:
            raise GatewayDenied("attempt_not_preparing", attempt.state.value)
        if grant.state is not GrantState.ACTIVE:
            raise GatewayDenied("grant_not_active", grant.state.value)
        if grant.action_type != intent.action_type:
            raise GatewayDenied("action_mismatch")
        if grant.target_type != intent.target_type or grant.target_id != intent.target_id:
            raise GatewayDenied("target_mismatch")
        actor = intent.actor_identity
        if not actor or actor != grant.actor_id:
            raise GatewayDenied("actor_mismatch")
        try:
            grant.validate_live(
                intent_hash=intent.intent_hash(),
                actor_id=actor,
                policy_binding=policy_binding,
                authorization_epoch=authorization_epoch,
            )
        except Exception as exc:
            reason = getattr(exc, "reason", "grant_invalid")
            raise GatewayDenied(str(reason), str(exc)) from exc

    def authorize_commit(
        self,
        *,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> EffectPermit:
        """Grant commit authority and return a single-use permit.

        REQUIRED effects are fenced first.  If the durable append fails, no
        permit exists and the grant is not spent (T1 fail-closed prerequisite).
        For fenced effects the durable RESERVED fact is the authoritative spend
        point; ``grant.spend()`` follows immediately in the same synchronous
        critical section.
        """

        policy = self._policy_for(intent)
        binding = policy.binding_hash()
        epoch = self._epoch.current
        self._validate_grant_identity(grant, attempt, intent, binding, epoch)

        # Final pre-authority kill check.  consume_permit() repeats this at the
        # actual mutation boundary (the hand-on-the-button rule).
        self._check_kill()

        now = self._clock()
        effect_id = secrets.token_urlsafe(16)
        semantic_key = intent.dedupe_key()
        fenced = policy.durability is DurabilityPolicy.REQUIRED

        if fenced:
            record = EffectLedgerRecord(
                effect_id=effect_id,
                semantic_key=semantic_key,
                state=EffectState.RESERVED,
                action_type=intent.action_type,
                intent_hash=intent.intent_hash(),
                policy_binding=binding,
                actor_id=intent.actor_identity,
                target_type=intent.target_type,
                target_id=intent.target_id,
                details={"attempt_id": attempt.attempt_id, "grant_id": grant.grant_id},
            )
            try:
                self._ledger.append_durable(record)
            except EffectLedgerError as exc:
                raise GatewayDenied("reservation_failed", str(exc)) from exc
            attempt.mark_reserved(grant)

        # Frozen spend rule: fenced => after durable reservation; non-fenced =>
        # this process-local transition itself is the authority grant point.
        grant.spend()

        return EffectPermit(
            grant_id=grant.grant_id,
            attempt_id=attempt.attempt_id,
            effect_id=effect_id,
            semantic_key=semantic_key,
            intent_hash=intent.intent_hash(),
            actor_id=intent.actor_identity or "",
            action_type=intent.action_type,
            target_type=intent.target_type,
            target_id=intent.target_id,
            policy_binding=binding,
            authorization_epoch=epoch,
            allowed_effects=policy.allowed_effects,
            fenced=fenced,
            issued_at=now,
            expires_at=now + self._permit_ttl,
        )

    def consume_permit(
        self,
        permit: EffectPermit,
        *,
        effect: EffectVerb,
        intent_hash: str,
        actor_id: str,
        target_type: str,
        target_id: str,
        policy_binding: str,
    ) -> None:
        """Validate and consume a permit immediately before external mutation.

        A denied validation leaves the permit unconsumed.  Layer 4 must call
        this in the same synchronous boundary as selecting the scoped broker
        primitive; no browser method may run first.
        """

        self._check_kill()
        now = self._clock()
        if permit.consumed:
            raise GatewayDenied("permit_reused")
        if now >= permit.expires_at:
            raise GatewayDenied("permit_expired")
        if permit.authorization_epoch != self._epoch.current:
            raise GatewayDenied("epoch_mismatch")
        if effect not in permit.allowed_effects:
            raise GatewayDenied("effect_not_allowed", effect.value)
        if intent_hash != permit.intent_hash:
            raise GatewayDenied("intent_mismatch")
        if actor_id != permit.actor_id:
            raise GatewayDenied("actor_mismatch")
        if target_type != permit.target_type or target_id != permit.target_id:
            raise GatewayDenied("target_mismatch")
        if policy_binding != permit.policy_binding:
            raise GatewayDenied("policy_mismatch")

        permit.consumed = True
        permit.consumed_effect = effect
        permit.consumed_at = now

    @staticmethod
    def _validate_outcome_objects(permit: EffectPermit, attempt: EffectAttempt) -> None:
        if permit.attempt_id != attempt.attempt_id:
            raise GatewayStateError("permit/attempt mismatch")
        if not permit.consumed:
            raise GatewayStateError("effect outcome cannot be recorded before permit consumption")

    def _terminal_record(
        self,
        permit: EffectPermit,
        state: EffectState,
        details: Optional[dict[str, Any]],
    ) -> EffectLedgerRecord:
        return EffectLedgerRecord(
            effect_id=permit.effect_id,
            semantic_key=permit.semantic_key,
            state=state,
            action_type=permit.action_type,
            intent_hash=permit.intent_hash,
            policy_binding=permit.policy_binding,
            actor_id=permit.actor_id,
            target_type=permit.target_type,
            target_id=permit.target_id,
            details={
                "attempt_id": permit.attempt_id,
                "grant_id": permit.grant_id,
                "permit_id": permit.permit_id,
                "effect": permit.consumed_effect.value if permit.consumed_effect else None,
                **(details or {}),
            },
        )

    def record_effect_confirmed(
        self,
        permit: EffectPermit,
        attempt: EffectAttempt,
        *,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record evidence-established success.

        Fenced effects close the durable reservation.  Non-fenced effects have
        no precommit fence and only update the in-memory attempt here.
        """

        self._validate_outcome_objects(permit, attempt)
        if permit.fenced:
            try:
                self._ledger.append_durable(
                    self._terminal_record(permit, EffectState.EFFECT_CONFIRMED, evidence)
                )
            except EffectLedgerError as exc:
                # The existing RESERVED remains unresolved and therefore safe
                # across restart; surface the recording failure rather than
                # pretending the terminal evidence became durable.
                raise GatewayStateError(f"could not persist confirmed outcome: {exc}") from exc
        attempt.mark_effect_confirmed()

    def record_effect_unknown(
        self,
        permit: EffectPermit,
        attempt: EffectAttempt,
        *,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        """Record an uncertain outcome; automatic retry is never authorized.

        Unknown outcomes are written durably even for non-fenced effects when
        the process is still alive, because the ledger is the authority for
        known uncertainty.  For a fenced effect, failure to append this terminal
        record still leaves RESERVED as an unresolved recovery blocker.
        """

        self._validate_outcome_objects(permit, attempt)
        attempt.mark_effect_unknown()
        try:
            self._ledger.append_durable(
                self._terminal_record(permit, EffectState.EFFECT_UNKNOWN, evidence)
            )
        except EffectLedgerError as exc:
            raise GatewayStateError(f"could not persist unknown outcome: {exc}") from exc
