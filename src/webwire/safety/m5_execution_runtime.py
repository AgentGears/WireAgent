"""M5 Layer 5 execution lifecycle orchestration.

This module is the trusted bridge between human-confirmed semantic intent and
Layer 4's least-authority surfaces. Capability code must receive only a
preparation authority or the narrow ``AuthorizedEffect.authority`` handle; the
runtime retains the grant, attempt, and exact permit receipt needed to close or
record the canonical effect lifecycle.

The runtime deliberately does not infer remote success from a browser click.
Callers must provide evidence and choose ``record_confirmed`` or
``record_unknown`` after authority has actually been consumed.
"""

from __future__ import annotations

import threading
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Optional

from webwire.safety.commit_gateway import CommitGateway, EffectPermit
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    EffectPolicyRegistry,
)
from webwire.safety.execution_models import (
    ApprovalGrant,
    ApprovalGrantStore,
    AttemptState,
    EffectAttempt,
    GrantClaimDenied,
    GrantState,
    GrantStateError,
)
from webwire.safety.models import WriteIntent
from webwire.safety.scoped_authority import (
    AuthorizedEffect,
    PostPreparationAuthority,
    QuotePreparationAuthority,
    ReplyPreparationAuthority,
    ScopedAuthorityBroker,
)

__all__ = [
    "M5ExecutionDenied",
    "M5ExecutionRuntime",
    "M5ExecutionSession",
    "M5ExecutionStateError",
]


class M5ExecutionDenied(RuntimeError):
    """Layer-5 execution authority could not be established."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"M5 execution denied: {reason}" + (f" — {detail}" if detail else "")
        )


class M5ExecutionStateError(RuntimeError):
    """Layer-5 orchestration attempted an invalid lifecycle transition."""


PreparationAuthority = (
    PostPreparationAuthority | ReplyPreparationAuthority | QuotePreparationAuthority
)


class _PruningApprovalGrantStore(ApprovalGrantStore):
    """Default Layer-5 registry with bounded terminal-grant retention.

    Execution sessions retain their exact grant object directly, so the registry
    does not need to keep SPENT/REVOKED or elapsed ACTIVE grants after a later
    approval is issued.  Pruning at the next mint preserves immediate ``get()``
    behavior while preventing a long-lived Dispatcher from accumulating one
    historical grant for every completed write.
    """

    def _prune_terminal(self) -> None:
        now = self._clock
        with self._lock:
            stale: list[str] = []
            sampled_now = now()
            for grant_id, grant in self._grants.items():
                with grant._lock:
                    if grant.state is not GrantState.ACTIVE or grant.is_expired(sampled_now):
                        stale.append(grant_id)
            for grant_id in stale:
                self._grants.pop(grant_id, None)

    def mint(
        self,
        *,
        intent_hash: str,
        actor_id: str,
        action_type: str,
        target_type: str,
        target_id: str,
        policy_binding: str,
        authorization_epoch: int,
    ) -> ApprovalGrant:
        self._prune_terminal()
        return super().mint(
            intent_hash=intent_hash,
            actor_id=actor_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            policy_binding=policy_binding,
            authorization_epoch=authorization_epoch,
        )


@dataclass
class M5ExecutionSession:
    """Trusted lifecycle owner for one human-approved semantic intent.

    One session owns one ApprovalGrant and a sequence of bounded EffectAttempts.
    A clean precommit attempt may close NO_EFFECT and release the grant claim;
    ``retry_clean_precommit`` then creates the next attempt against the same
    still-ACTIVE grant. Once commit authority is issued the approval is SPENT and
    this session can never manufacture a replacement attempt.
    """

    _gateway: CommitGateway
    _scoped: ScopedAuthorityBroker
    _policies: EffectPolicyRegistry
    _grant: ApprovalGrant
    _intent: WriteIntent
    _attempt: EffectAttempt
    _authorized: Optional[AuthorizedEffect] = None
    _lock: Any = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    @property
    def grant(self) -> ApprovalGrant:
        return self._grant

    @property
    def attempt(self) -> EffectAttempt:
        return self._attempt

    @property
    def authorized_effect(self) -> Optional[AuthorizedEffect]:
        with self._lock:
            return self._authorized

    @property
    def intent_hash(self) -> str:
        return self._intent.intent_hash()

    @property
    def action_type(self) -> str:
        return self._intent.action_type

    def prepare(self) -> PreparationAuthority:
        """Return the narrow preparation authority for the current attempt."""
        with self._lock:
            if self._authorized is not None:
                raise M5ExecutionStateError(
                    "preparation cannot be reopened after effect authority is scoped"
                )
            attempt = self._attempt
        return self._scoped.prepare(
            grant=self._grant,
            attempt=attempt,
            intent=self._intent,
        )

    def scope_effect(self) -> AuthorizedEffect:
        """Create at most one trusted effect receipt for the current attempt."""
        with self._lock:
            if self._authorized is not None:
                return self._authorized
            authorized = self._scoped.scope_effect(
                grant=self._grant,
                attempt=self._attempt,
                intent=self._intent,
            )
            self._authorized = authorized
            return authorized

    def resolve_no_external_effect(self, *, reason: str) -> None:
        """Close the current attempt when the canonical mutation did not occur.

        Before permit issue this is a clean precommit NO_EFFECT and the approval
        remains ACTIVE within its bounded retry budget. After permit issue the
        approval is already SPENT, so the CommitGateway performs the explicit
        issued-but-unconsumed closure. A consumed permit can never take this
        path. Ambiguous reservation I/O without an issued permit is deliberately
        left unresolved; only gateway-owned durable evidence can close it.
        """
        if not isinstance(reason, str) or not reason.strip():
            raise M5ExecutionStateError("NO_EFFECT resolution requires a reason")
        reason = reason.strip()
        with self._lock:
            authorized = self._authorized
            attempt = self._attempt

            if authorized is None:
                self._close_clean_precommit_unlocked(attempt)
                return

            permit = authorized.permit
            if permit is None:
                self._close_clean_precommit_unlocked(attempt)
                return
            if permit.consumed:
                raise M5ExecutionStateError(
                    "consumed mutation authority cannot be resolved as NO_EFFECT"
                )
            if attempt.state is AttemptState.NO_EFFECT:
                # Permit expiry may have already closed and evicted the exact
                # permit while the Layer-4 holder still references its receipt.
                return
            self._gateway.close_unconsumed_permit_no_effect(
                permit,
                reason=reason,
            )

    def _close_clean_precommit_unlocked(self, attempt: EffectAttempt) -> None:
        if attempt.state is AttemptState.NO_EFFECT:
            return
        if attempt.state is not AttemptState.PREPARING:
            raise M5ExecutionStateError(
                f"clean NO_EFFECT requires PREPARING, got {attempt.state.value}"
            )
        if attempt.reservation_started:
            raise M5ExecutionStateError(
                "reservation I/O started without an issued permit; state is unresolved"
            )
        try:
            attempt.mark_no_effect(self._grant)
        except GrantStateError as exc:
            raise M5ExecutionStateError(str(exc)) from exc

    def retry_clean_precommit(self) -> EffectAttempt:
        """Claim the next bounded attempt under the same ACTIVE approval."""
        with self._lock:
            authorized = self._authorized
            if authorized is not None and authorized.permit is not None:
                raise M5ExecutionStateError(
                    "commit authority was issued; start a new human approval instead"
                )
            if self._attempt.state is not AttemptState.NO_EFFECT:
                raise M5ExecutionStateError(
                    "retry requires the previous attempt to be clean NO_EFFECT"
                )
            if self._grant.state is not GrantState.ACTIVE:
                raise M5ExecutionStateError(
                    f"retry requires ACTIVE approval, got {self._grant.state.value}"
                )
            attempt = EffectAttempt(grant_id=self._grant.grant_id)
            policy_binding = self._current_policy_binding()
            try:
                self._grant.claim(
                    attempt.attempt_id,
                    intent_hash=self._intent.intent_hash(),
                    actor_id=self._intent.actor_identity or "",
                    policy_binding=policy_binding,
                    authorization_epoch=self._gateway.authorization_epoch,
                )
            except GrantClaimDenied as exc:
                raise M5ExecutionDenied(exc.reason, str(exc)) from exc
            self._attempt = attempt
            self._authorized = None
            return attempt

    def record_confirmed(self, *, evidence: Optional[dict[str, Any]] = None) -> None:
        """Persist EFFECT_CONFIRMED for the exact consumed receipt."""
        authorized, permit = self._require_consumed_receipt()
        self._gateway.record_effect_confirmed(
            permit,
            authorized.attempt,
            evidence=evidence,
        )

    def record_unknown(self, *, evidence: Optional[dict[str, Any]] = None) -> None:
        """Persist EFFECT_UNKNOWN for the exact consumed receipt."""
        authorized, permit = self._require_consumed_receipt()
        self._gateway.record_effect_unknown(
            permit,
            authorized.attempt,
            evidence=evidence,
        )

    def _require_consumed_receipt(self) -> tuple[AuthorizedEffect, EffectPermit]:
        with self._lock:
            authorized = self._authorized
            if authorized is None:
                raise M5ExecutionStateError("no effect authority has been scoped")
            permit = authorized.permit
            if permit is None:
                raise M5ExecutionStateError("no EffectPermit was issued")
            if not permit.consumed:
                raise M5ExecutionStateError("EffectPermit was not consumed")
            if authorized.attempt is not self._attempt:
                raise M5ExecutionStateError("receipt attempt is not the current attempt")
            return authorized, permit

    def _current_policy_binding(self) -> str:
        try:
            policy = self._policies.require(self._intent.action_type)
        except KeyError as exc:
            raise M5ExecutionDenied("policy_missing", str(exc)) from exc
        policy.validate()
        return policy.binding_hash()


class M5ExecutionRuntime:
    """Factory for trusted Layer-5 execution sessions."""

    def __init__(
        self,
        *,
        scoped_authority: ScopedAuthorityBroker,
        commit_gateway: CommitGateway,
        grants: Optional[ApprovalGrantStore] = None,
        policies: EffectPolicyRegistry = DEFAULT_EFFECT_POLICIES,
    ) -> None:
        gateway_policies = getattr(commit_gateway, "_policies", None)
        if gateway_policies is not policies:
            raise M5ExecutionStateError("runtime/gateway policy registry mismatch")
        self._scoped = scoped_authority
        self._gateway = commit_gateway
        self._grants = grants if grants is not None else _PruningApprovalGrantStore()
        self._policies = policies

    def issue(self, intent: WriteIntent) -> M5ExecutionSession:
        """Mint one approval grant after human confirmation and claim attempt 1."""
        frozen = deepcopy(intent)
        actor_id = frozen.actor_identity or ""
        if not actor_id:
            raise M5ExecutionDenied("actor_missing")
        if not frozen.target_type or not frozen.target_id:
            raise M5ExecutionDenied("target_missing")
        try:
            policy = self._policies.require(frozen.action_type)
        except KeyError as exc:
            raise M5ExecutionDenied("policy_missing", str(exc)) from exc
        policy.validate()
        binding = policy.binding_hash()
        epoch = self._gateway.authorization_epoch
        grant = self._grants.mint(
            intent_hash=frozen.intent_hash(),
            actor_id=actor_id,
            action_type=frozen.action_type,
            target_type=frozen.target_type,
            target_id=frozen.target_id,
            policy_binding=binding,
            authorization_epoch=epoch,
        )
        attempt = EffectAttempt(grant_id=grant.grant_id)
        try:
            grant.claim(
                attempt.attempt_id,
                intent_hash=frozen.intent_hash(),
                actor_id=actor_id,
                policy_binding=binding,
                authorization_epoch=epoch,
            )
        except GrantClaimDenied as exc:
            raise M5ExecutionDenied(exc.reason, str(exc)) from exc
        return M5ExecutionSession(
            _gateway=self._gateway,
            _scoped=self._scoped,
            _policies=self._policies,
            _grant=grant,
            _intent=frozen,
            _attempt=attempt,
        )
