"""M5 ApprovalGrant / EffectAttempt models — the two execution lifecycle
state machines (frozen spec: docs/M5_DESIGN.md sections 5-7).

Scope of THIS module (layer 2 of the M5 build order): the models and their
transitions only. The Commit Gateway (layer 3), scoped authorities (layer 4),
and RecoveryGuard (layer 6) are deliberately absent — they compose these
models; they do not live here.

The two machines:

ApprovalGrant (ephemeral, in-memory, never persisted)
    ACTIVE → SPENT | EXPIRED | REVOKED            (terminal, one-directional)
    orthogonal claim lock: claimed_by = attempt_id (CAS; NOT a state)

EffectAttempt (one per execution try)
    PREPARING → NO_EFFECT | RESERVED
    RESERVED  → EFFECT_CONFIRMED | EFFECT_UNKNOWN
    unfenced effects may skip RESERVED entirely (spec 5.2).

The frozen spend rule (spec 6), implemented as TWO separate operations the
gateway composes under the claim lock:

    durable EFFECT_RESERVED append + fsync      (gateway, layer 3)
        then grant.spend()                      (this module)

Nothing here performs I/O. The ledger-fact-dominates rule means a crashed
process cannot resurrect a grant anyway — grants die with the process, and
the ledger's unresolved reservation blocks replay (RecoveryGuard, layer 6).

Ephemerality is a design decision, not a gap: durable safety state belongs
to the EffectLedger, never to persisted confirmation tokens.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Callable, Optional

__all__ = [
    "GrantState",
    "AttemptState",
    "ApprovalGrantError",
    "GrantClaimDenied",
    "GrantStateError",
    "AuthorizationEpoch",
    "ApprovalGrant",
    "EffectAttempt",
    "ApprovalGrantStore",
]

# Human-approval validity (matches the WriteKernel confirmation TTL — one
# approval, one working window). Injected clocks keep this testable.
DEFAULT_GRANT_TTL_S = 300.0

# Spec 5.3: a clean precommit failure does not consume the approval, but it
# also does not license an unbounded loop. Default attempt budget.
DEFAULT_MAX_PRECOMMIT_ATTEMPTS = 3


class GrantState(StrEnum):
    ACTIVE = "active"
    SPENT = "spent"
    EXPIRED = "expired"
    REVOKED = "revoked"


class AttemptState(StrEnum):
    PREPARING = "preparing"
    NO_EFFECT = "no_effect"
    RESERVED = "reserved"
    EFFECT_CONFIRMED = "effect_confirmed"
    EFFECT_UNKNOWN = "effect_unknown"


class ApprovalGrantError(Exception):
    """Base for the execution-authority lifecycle errors."""


class GrantClaimDenied(ApprovalGrantError):
    """A claim or validation was refused. The grant is untouched."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"claim denied: {reason}" + (f" — {detail}" if detail else ""))


class GrantStateError(ApprovalGrantError):
    """An illegal transition or terminal-state re-entry was attempted."""


class AuthorizationEpoch:
    """Monotonic authorization epoch (spec 7).

    Grants bind the epoch at issue; bumping the epoch (kill switch trip, or
    any future operator-wide authorization reset) invalidates every grant
    from earlier epochs without traversing them. Persistence of the counter
    belongs to the ledger wiring (later layers); the comparison semantics
    live here.
    """

    def __init__(self, initial: int = 0) -> None:
        self._current = initial

    @property
    def current(self) -> int:
        return self._current

    def bump(self) -> int:
        """Advance the epoch. Every grant from earlier epochs is invalid
        from this instant."""
        self._current += 1
        return self._current


@dataclass
class ApprovalGrant:
    """One human approval. Ephemeral; the ledger owns durable safety state.

    Bindings (validated on every claim — the T10/T11 prerequisites):
    intent_hash, actor_id, action_type/target, policy_binding (the
    EffectPolicy.binding_hash() from layer 1), authorization_epoch.
    """

    intent_hash: str
    actor_id: str
    action_type: str
    target_type: str
    target_id: str
    policy_binding: str
    authorization_epoch: int
    grant_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0  # set by the store's clock at mint; 0 = never
    max_precommit_attempts: int = DEFAULT_MAX_PRECOMMIT_ATTEMPTS
    state: GrantState = GrantState.ACTIVE
    claimed_by: Optional[str] = None
    precommit_attempts: int = 0
    # The store's injected clock, carried so expiry checks use the same
    # time source the grant was minted with (test determinism). Not part
    # of identity; grants are ephemeral and never serialized.
    clock: Callable[[], float] = field(default_factory=lambda: time.time, repr=False, compare=False)

    # -- inspection ---------------------------------------------------------

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else self.clock()
        return self.expires_at > 0 and t >= self.expires_at

    def _refresh_state(self, now: Optional[float] = None) -> None:
        """Lazy EXPIRED demotion (time-driven, not event-driven)."""
        if self.state is GrantState.ACTIVE and self.is_expired(now):
            self.state = GrantState.EXPIRED

    # -- validation (the claim prerequisites) --------------------------------

    def validate_live(
        self,
        *,
        intent_hash: str,
        actor_id: str,
        authorization_epoch: int,
        now: Optional[float] = None,
    ) -> None:
        """Raise GrantClaimDenied unless this grant is live for exactly this
        intent/actor/epoch. Never mutates except the lazy EXPIRED demotion
        and epoch-driven REVOKED (spec 7: epoch mismatch revokes)."""
        self._refresh_state(now)
        if self.state is GrantState.EXPIRED:
            raise GrantClaimDenied("expired")
        if self.state is GrantState.SPENT:
            raise GrantClaimDenied("spent")
        if self.state is GrantState.REVOKED:
            raise GrantClaimDenied("revoked")
        if authorization_epoch != self.authorization_epoch:
            self.state = GrantState.REVOKED
            raise GrantClaimDenied(
                "epoch_mismatch",
                f"grant epoch {self.authorization_epoch} != current {authorization_epoch}",
            )
        if intent_hash != self.intent_hash:
            raise GrantClaimDenied("intent_mismatch")
        if actor_id != self.actor_id:
            raise GrantClaimDenied("actor_mismatch")

    # -- the orthogonal claim lock (spec 5.1: CAS, not a state) --------------

    def claim(
        self,
        attempt_id: str,
        *,
        intent_hash: str,
        actor_id: str,
        authorization_epoch: int,
        now: Optional[float] = None,
    ) -> None:
        """Compare-and-set the claim: exactly one live attempt per grant.

        Denials (grant untouched unless noted):
        - state/expiry/epoch/intent/actor failures — validate_live()
        - already claimed by another attempt — approval_already_claimed
        - precommit attempt budget exhausted — attempts_exhausted (the grant
          stays ACTIVE; only a human may start over — spec 5.3)
        """
        self.validate_live(
            intent_hash=intent_hash,
            actor_id=actor_id,
            authorization_epoch=authorization_epoch,
            now=now,
        )
        if self.claimed_by is not None and self.claimed_by != attempt_id:
            raise GrantClaimDenied(
                "approval_already_claimed", f"claimed by {self.claimed_by!r}"
            )
        if self.claimed_by is None and self.precommit_attempts >= self.max_precommit_attempts:
            raise GrantClaimDenied(
                "attempts_exhausted",
                f"{self.precommit_attempts} precommit attempts used of "
                f"{self.max_precommit_attempts}",
            )
        self.claimed_by = attempt_id

    def release_claim(self, attempt_id: str) -> None:
        """Release the claim after a PROVEN no-effect outcome. The grant
        stays ACTIVE (the frozen rule: clean precommit failure does not
        consume approval). Only the owning attempt may release."""
        if self.claimed_by != attempt_id:
            raise GrantStateError(
                f"release_claim called by {attempt_id!r} but claim is held by "
                f"{self.claimed_by!r}"
            )
        self.claimed_by = None
        self.precommit_attempts += 1

    # -- terminal transitions -------------------------------------------------

    def spend(self) -> None:
        """ACTIVE → SPENT. Terminal and irrevocable.

        ORDERING CONTRACT (spec 6.1): for fenced effects the gateway calls
        this immediately AFTER the durable EFFECT_RESERVED append + fsync,
        while still holding the claim. For non-fenced effects the gateway
        calls it as the process-local CAS at the commit boundary. Either
        way, this module never performs the I/O — it records the fact that
        execution authority has been granted.
        """
        if self.state is not GrantState.ACTIVE:
            raise GrantStateError(f"spend() requires ACTIVE, got {self.state.value}")
        self.state = GrantState.SPENT

    def revoke(self) -> None:
        """ACTIVE → REVOKED (operator action; epoch bumps do this lazily on
        validation). Terminal."""
        if self.state is not GrantState.ACTIVE:
            raise GrantStateError(f"revoke() requires ACTIVE, got {self.state.value}")
        self.state = GrantState.REVOKED


@dataclass
class EffectAttempt:
    """One execution try against a grant (spec 5.2).

    Transitions:
      PREPARING → NO_EFFECT               (proven; releases the grant claim)
      PREPARING → RESERVED                (durable fence established; the
                                           gateway spends the grant after the
                                           fsync, not this method)
      RESERVED → EFFECT_CONFIRMED
      RESERVED → EFFECT_UNKNOWN
      PREPARING → EFFECT_CONFIRMED / EFFECT_UNKNOWN
                (unfenced effects skip RESERVED — spec 5.2; the gateway
                enforces fencing order, the model records outcomes)
    NO_EFFECT and the three outcome states are terminal for the attempt.
    """

    grant_id: str
    attempt_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    state: AttemptState = AttemptState.PREPARING

    def _require(self, expected: AttemptState) -> None:
        if self.state is not expected:
            raise GrantStateError(
                f"attempt {self.attempt_id!r}: transition requires "
                f"{expected.value}, got {self.state.value}"
            )

    def mark_no_effect(self, grant: ApprovalGrant) -> None:
        """PROVEN no external effect was possible: release the grant's claim;
        the grant remains ACTIVE (the T13 path)."""
        self._require(AttemptState.PREPARING)
        grant.release_claim(self.attempt_id)
        self.state = AttemptState.NO_EFFECT

    def mark_reserved(self, grant: ApprovalGrant) -> None:
        """The durable reservation exists. The gateway — NOT this method —
        spends the grant immediately after the fsync (spec 6.1 ordering)."""
        self._require(AttemptState.PREPARING)
        if grant.claimed_by != self.attempt_id:
            raise GrantStateError(
                "mark_reserved requires this attempt to hold the grant claim"
            )
        self.state = AttemptState.RESERVED

    def mark_effect_confirmed(self) -> None:
        """External evidence establishes the effect (terminal)."""
        if self.state not in (AttemptState.RESERVED, AttemptState.PREPARING):
            self._require(AttemptState.RESERVED)
        self.state = AttemptState.EFFECT_CONFIRMED

    def mark_effect_unknown(self) -> None:
        """The effect may have happened; retry is forbidden until
        reconciliation (terminal — invariant 9)."""
        if self.state not in (AttemptState.RESERVED, AttemptState.PREPARING):
            self._require(AttemptState.RESERVED)
        self.state = AttemptState.EFFECT_UNKNOWN


class ApprovalGrantStore:
    """Ephemeral, in-memory grant registry (the _pending_tokens pattern,
    formalized). Dies with the process BY DESIGN — durable safety state
    lives in the EffectLedger (layer 1), never here."""

    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        ttl_seconds: float = DEFAULT_GRANT_TTL_S,
        max_precommit_attempts: int = DEFAULT_MAX_PRECOMMIT_ATTEMPTS,
    ) -> None:
        self._clock = clock
        self._ttl = ttl_seconds
        self._max_attempts = max_precommit_attempts
        self._grants: dict[str, ApprovalGrant] = {}

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
        """Issue a grant. Callers hand the grant_id to the human-facing
        confirmation flow; the grant itself never leaves the process."""
        grant = ApprovalGrant(
            intent_hash=intent_hash,
            actor_id=actor_id,
            action_type=action_type,
            target_type=target_type,
            target_id=target_id,
            policy_binding=policy_binding,
            authorization_epoch=authorization_epoch,
            expires_at=self._clock() + self._ttl,
            max_precommit_attempts=self._max_attempts,
            clock=self._clock,
        )
        self._grants[grant.grant_id] = grant
        return grant

    def get(self, grant_id: str) -> Optional[ApprovalGrant]:
        return self._grants.get(grant_id)

    def __len__(self) -> int:
        return len(self._grants)
