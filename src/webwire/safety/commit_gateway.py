"""M5 layer 3 — Commit Gateway and single-use EffectPermit.

The gateway composes EffectPolicy, EffectLedger, ApprovalGrant, EffectAttempt,
KillSwitch, and AuthorizationEpoch. It deliberately does not expose scoped
broker authority objects; those land in layer 4.

A crash after a fenced reservation and before a terminal record leaves raw
RESERVED evidence; EffectLedger recovery projects it as EFFECT_UNKNOWN. No
exactly-once claim is made.
"""

from __future__ import annotations

import secrets
import threading
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
    GrantClaimDenied,
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
_RESERVED_DETAIL_KEYS = frozenset({"attempt_id", "grant_id", "permit_id", "effect"})


class GatewayDenied(RuntimeError):
    """Commit authority was denied before the requested mutation boundary."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"gateway denied: {reason}" + (f" — {detail}" if detail else ""))


class GatewayStateError(RuntimeError):
    """Gateway/outcome API was called with an inconsistent lifecycle object."""


@dataclass
class _PermitUse:
    consumed: bool = False
    consumed_effect: Optional[EffectVerb] = None
    consumed_at: Optional[float] = None


@dataclass(frozen=True)
class EffectPermit:
    """Immutable, process-local authority descended from one ApprovalGrant."""

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
    _use: _PermitUse = field(default_factory=_PermitUse, repr=False, compare=False)

    @property
    def consumed(self) -> bool:
        return self._use.consumed

    @property
    def consumed_effect(self) -> Optional[EffectVerb]:
        return self._use.consumed_effect

    @property
    def consumed_at(self) -> Optional[float]:
        return self._use.consumed_at


class CommitGateway:
    """The M5 process-local commit-authority boundary.

    Lock order for authority transitions is:

        gateway protocol lock
          -> kill execution fence
          -> policy registry fence
          -> approval-grant claim fence

    Kill-listener draining happens before the protocol lock. This avoids a
    callback lock inversion while ``execution_fence`` closes the race between
    preflight and the authority transition.
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
        if permit_ttl_seconds <= 0:
            raise ValueError("permit_ttl_seconds must be > 0")
        self._ledger = ledger
        self._kill = kill_switch
        self._epoch = authorization_epoch
        self._policies = policies
        self._clock = clock
        self._permit_ttl = permit_ttl_seconds
        self._issued_permits: dict[str, EffectPermit] = {}
        self._issued_attempts: dict[str, EffectAttempt] = {}
        self._protocol_lock = threading.RLock()
        # Revocation is safety-critical. If delivery is pending, the kill
        # execution fence stays closed even if an earlier listener reset the
        # live kill flag before this callback runs.
        self._kill.add_trip_listener(self._epoch.bump, critical=True)

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

    def _current_policy_binding(self, action_type: str) -> str:
        try:
            policy = self._policies.require(action_type)
        except KeyError as exc:
            raise GatewayDenied("policy_missing", str(exc)) from exc
        policy.validate()
        return policy.binding_hash()

    @staticmethod
    def _deny_if_killed(blocked: bool) -> None:
        if blocked:
            raise GatewayDenied("kill_switch")

    def _preflight_kill_notifications(self) -> None:
        """Drain listener obligations before taking the gateway protocol lock."""
        self._kill.tripped()

    def _require_issued_permit(self, permit: EffectPermit) -> None:
        canonical = self._issued_permits.get(permit.permit_id)
        if canonical is None:
            if permit.consumed:
                raise GatewayDenied("permit_reused")
            raise GatewayDenied("permit_unknown")
        if canonical is not permit:
            raise GatewayDenied("permit_unknown")

    def _canonical_attempt(self, permit: EffectPermit) -> EffectAttempt:
        attempt = self._issued_attempts.get(permit.permit_id)
        if attempt is None:
            raise GatewayStateError("issued permit lost canonical attempt lineage")
        return attempt

    def _evict_permit(self, permit: EffectPermit) -> None:
        self._issued_permits.pop(permit.permit_id, None)
        self._issued_attempts.pop(permit.permit_id, None)

    def _close_expired_unconsumed(self, permit: EffectPermit) -> None:
        """Close unused authority without leaving false uncertainty.

        The permit was already capable of producing an effect, so the human
        approval remains SPENT. Expiry proves this exact permit never crossed
        the mutation boundary; the canonical attempt therefore terminalizes as
        NO_EFFECT without releasing/reusing the approval.
        """
        if permit.consumed:
            raise GatewayStateError("cannot expiry-close a consumed permit")
        attempt = self._canonical_attempt(permit)
        if permit.fenced:
            try:
                self._ledger.append_durable(
                    self._terminal_record(
                        permit,
                        EffectState.NO_EFFECT,
                        {"reason": "permit_expired"},
                    )
                )
            except EffectLedgerError as exc:
                raise GatewayDenied("expiry_close_failed", str(exc)) from exc
        attempt.mark_no_effect_after_authority()
        self._evict_permit(permit)

    def _prune_expired_unconsumed(self, now: float) -> None:
        expired = [
            permit
            for permit in self._issued_permits.values()
            if not permit.consumed and now >= permit.expires_at
        ]
        for permit in expired:
            self._close_expired_unconsumed(permit)

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
        except GrantClaimDenied as exc:
            raise GatewayDenied(exc.reason, str(exc)) from exc

    def authorize_commit(
        self,
        *,
        grant: ApprovalGrant,
        attempt: EffectAttempt,
        intent: WriteIntent,
    ) -> EffectPermit:
        """Grant commit authority and mint one exact single-use permit."""
        self._preflight_kill_notifications()
        with self._protocol_lock:
            with self._kill.execution_fence() as blocked:
                self._deny_if_killed(blocked)
                now = self._clock()
                self._prune_expired_unconsumed(now)
                try:
                    policy_fence = self._policies.policy_fence(intent.action_type)
                    with policy_fence as policy:
                        binding = policy.binding_hash()
                        epoch = self._epoch.current
                        with grant.claim_fence(attempt.attempt_id):
                            self._validate_grant_identity(
                                grant,
                                attempt,
                                intent,
                                binding,
                                epoch,
                            )

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
                                    details={
                                        "attempt_id": attempt.attempt_id,
                                        "grant_id": grant.grant_id,
                                    },
                                )
                                try:
                                    self._ledger.append_durable(record)
                                except EffectLedgerError as exc:
                                    raise GatewayDenied(
                                        "reservation_failed",
                                        str(exc),
                                    ) from exc
                                attempt.mark_reserved(grant)

                            grant.spend()
                            permit = EffectPermit(
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
                            self._issued_permits[permit.permit_id] = permit
                            self._issued_attempts[permit.permit_id] = attempt
                            return permit
                except GrantClaimDenied as exc:
                    raise GatewayDenied(exc.reason, str(exc)) from exc
                except KeyError as exc:
                    raise GatewayDenied("policy_missing", str(exc)) from exc

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
        """Atomically validate and consume a gateway-issued permit."""
        self._preflight_kill_notifications()
        with self._protocol_lock:
            with self._kill.execution_fence() as blocked:
                self._deny_if_killed(blocked)
                self._require_issued_permit(permit)
                now = self._clock()
                if permit.consumed:
                    raise GatewayDenied("permit_reused")
                if now >= permit.expires_at:
                    self._close_expired_unconsumed(permit)
                    raise GatewayDenied("permit_expired")
                if permit.authorization_epoch != self._epoch.current:
                    raise GatewayDenied("epoch_mismatch")

                try:
                    policy_fence = self._policies.policy_fence(permit.action_type)
                    with policy_fence as current_policy:
                        current_binding = self._current_policy_binding(
                            permit.action_type
                        )
                        assert current_binding == current_policy.binding_hash()
                        if current_binding != permit.policy_binding:
                            raise GatewayDenied(
                                "policy_mismatch",
                                "registered policy changed after permit mint",
                            )
                        if policy_binding != permit.policy_binding:
                            raise GatewayDenied("policy_mismatch")
                        if effect not in permit.allowed_effects:
                            raise GatewayDenied("effect_not_allowed", effect.value)
                        if intent_hash != permit.intent_hash:
                            raise GatewayDenied("intent_mismatch")
                        if actor_id != permit.actor_id:
                            raise GatewayDenied("actor_mismatch")
                        if target_type != permit.target_type or target_id != permit.target_id:
                            raise GatewayDenied("target_mismatch")

                        permit._use.consumed = True
                        permit._use.consumed_effect = effect
                        permit._use.consumed_at = now
                except KeyError as exc:
                    raise GatewayDenied("policy_missing", str(exc)) from exc

    def _validate_outcome_objects(
        self,
        permit: EffectPermit,
        attempt: EffectAttempt,
    ) -> None:
        self._require_issued_permit(permit)
        canonical_attempt = self._canonical_attempt(permit)
        if canonical_attempt is not attempt:
            raise GatewayStateError("permit requires its canonical EffectAttempt object")
        if permit.attempt_id != attempt.attempt_id:
            raise GatewayStateError("permit/attempt mismatch")
        if permit.grant_id != attempt.grant_id:
            raise GatewayStateError("permit/attempt grant mismatch")
        if not permit.consumed:
            raise GatewayStateError(
                "effect outcome cannot be recorded before permit consumption"
            )
        expected = AttemptState.RESERVED if permit.fenced else AttemptState.PREPARING
        if attempt.state is not expected:
            raise GatewayStateError(
                f"effect outcome requires {expected.value}, got {attempt.state.value}"
            )

    def _terminal_record(
        self,
        permit: EffectPermit,
        state: EffectState,
        details: Optional[dict[str, Any]],
    ) -> EffectLedgerRecord:
        evidence = dict(details or {})
        overlap = _RESERVED_DETAIL_KEYS.intersection(evidence)
        if overlap:
            names = ", ".join(sorted(overlap))
            raise GatewayStateError(
                f"evidence contains reserved ledger detail keys: {names}"
            )
        evidence.update(
            {
                "attempt_id": permit.attempt_id,
                "grant_id": permit.grant_id,
                "permit_id": permit.permit_id,
                "effect": (
                    permit.consumed_effect.value
                    if permit.consumed_effect is not None
                    else None
                ),
            }
        )
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
            details=evidence,
        )

    def record_effect_confirmed(
        self,
        permit: EffectPermit,
        attempt: EffectAttempt,
        *,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        """Durably record every known confirmed effect, fenced or not."""
        with self._protocol_lock:
            self._validate_outcome_objects(permit, attempt)
            try:
                self._ledger.append_durable(
                    self._terminal_record(
                        permit,
                        EffectState.EFFECT_CONFIRMED,
                        evidence,
                    )
                )
            except EffectLedgerError as exc:
                raise GatewayStateError(
                    f"could not persist confirmed outcome: {exc}"
                ) from exc
            attempt.mark_effect_confirmed()
            self._evict_permit(permit)

    def record_effect_unknown(
        self,
        permit: EffectPermit,
        attempt: EffectAttempt,
        *,
        evidence: Optional[dict[str, Any]] = None,
    ) -> None:
        """Persist uncertainty before making the in-memory attempt terminal."""
        with self._protocol_lock:
            self._validate_outcome_objects(permit, attempt)
            try:
                self._ledger.append_durable(
                    self._terminal_record(
                        permit,
                        EffectState.EFFECT_UNKNOWN,
                        evidence,
                    )
                )
            except EffectLedgerError as exc:
                raise GatewayStateError(
                    f"could not persist unknown outcome: {exc}"
                ) from exc
            attempt.mark_effect_unknown()
            self._evict_permit(permit)
