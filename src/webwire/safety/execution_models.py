"""M5 ApprovalGrant / EffectAttempt execution lifecycle models.

Scope of this module: the two in-memory state machines and their synchronization
primitives. The Commit Gateway (layer 3), scoped authorities (layer 4), and
RecoveryGuard (layer 6) compose these models; they do not live here.

ApprovalGrant (ephemeral, in-memory, never persisted)
    ACTIVE -> SPENT | EXPIRED | REVOKED            (terminal, one-directional)
    orthogonal claim lock: claimed_by = attempt_id (atomic CAS; NOT a state)

EffectAttempt (one per execution try)
    PREPARING -> NO_EFFECT | RESERVED
    RESERVED  -> NO_EFFECT | EFFECT_CONFIRMED | EFFECT_UNKNOWN
    unfenced effects may skip RESERVED entirely.

All public model fields are read-only after construction. Bindings and lineage
are immutable, while lifecycle state can change only through the transition
methods in this module. The implementation is an engineering boundary against
accidental state-machine bypass, not a security sandbox against hostile Python
that deliberately uses reflection or ``object.__setattr__``.

Each EffectAttempt owns one stable ``effect_id`` before it reaches the Commit
Gateway. That durable lineage identity survives an ambiguous reservation write:
retrying authorization for the same attempt reuses the same effect fact rather
than creating a second reservation.

``reservation_started`` is an orthogonal protocol latch, not a canonical effect
state. Once REQUIRED reservation I/O has begun, the generic clean-precommit
NO_EFFECT path is forbidden: an append can have written bytes before reporting
failure, so only gateway-owned durable closure may subsequently prove that the
attempt can be abandoned.

There are two distinct ways to reach NO_EFFECT:
- clean precommit failure before reservation I/O: release the claim and preserve
  ACTIVE approval;
- unused authority expiry: approval is already SPENT, so only the attempt is
  terminalized. The claim is never released back into reusable authority.

For fenced effects the gateway composes the spend protocol while holding the
grant's claim fence:

    attempt.begin_reservation(grant)
        -> durable EFFECT_RESERVED append + fsync
        -> attempt.mark_reserved(grant)
        -> grant.spend()

Nothing here performs I/O. Durable safety state belongs to EffectLedger; grants
and attempts are process-local lifecycle records.
"""

from __future__ import annotations

import secrets
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Callable, Iterator, Optional

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

DEFAULT_GRANT_TTL_S = 300.0
DEFAULT_MAX_PRECOMMIT_ATTEMPTS = 3

_GRANT_PUBLIC_FIELDS = frozenset(
    {
        "intent_hash",
        "actor_id",
        "action_type",
        "target_type",
        "target_id",
        "policy_binding",
        "authorization_epoch",
        "grant_id",
        "issued_at",
        "expires_at",
        "max_precommit_attempts",
        "state",
        "claimed_by",
        "precommit_attempts",
        "clock",
    }
)
_ATTEMPT_PUBLIC_FIELDS = frozenset(
    {"grant_id", "attempt_id", "effect_id", "state", "reservation_started"}
)


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
    """Base for execution-authority lifecycle errors."""


class GrantClaimDenied(ApprovalGrantError):
    """A claim or validation was refused."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"claim denied: {reason}" + (f" — {detail}" if detail else ""))


class GrantStateError(ApprovalGrantError):
    """An illegal transition or direct mutation of model state was attempted."""


class AuthorizationEpoch:
    """Thread-safe monotonic authorization epoch.

    Grants bind the epoch at issue; bumping it invalidates every grant and
    permit from an earlier epoch. Persistence of the epoch value is a later
    wiring concern; comparison and process-local linearization live here.
    """

    def __init__(self, initial: int = 0) -> None:
        self._current = initial
        self._lock = threading.RLock()

    @property
    def current(self) -> int:
        with self._lock:
            return self._current

    def bump(self) -> int:
        """Advance the epoch and return the new value."""
        with self._lock:
            self._current += 1
            return self._current


@dataclass
class ApprovalGrant:
    """One human approval with sealed fields and an atomic claim lock."""

    intent_hash: str
    actor_id: str
    action_type: str
    target_type: str
    target_id: str
    policy_binding: str
    authorization_epoch: int
    grant_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    issued_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    max_precommit_attempts: int = DEFAULT_MAX_PRECOMMIT_ATTEMPTS
    state: GrantState = GrantState.ACTIVE
    claimed_by: Optional[str] = None
    precommit_attempts: int = 0
    clock: Callable[[], float] = field(
        default_factory=lambda: time.time,
        repr=False,
        compare=False,
    )
    _lock: Any = field(
        default_factory=threading.RLock,
        init=False,
        repr=False,
        compare=False,
    )

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _GRANT_PUBLIC_FIELDS and name in self.__dict__:
            raise GrantStateError(
                f"approval field {name!r} is read-only; use lifecycle methods"
            )
        object.__setattr__(self, name, value)

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else self.clock()
        return self.expires_at > 0 and t >= self.expires_at

    def _refresh_state(self, now: Optional[float] = None) -> None:
        if self.state is GrantState.ACTIVE and self.is_expired(now):
            object.__setattr__(self, "state", GrantState.EXPIRED)

    def validate_live(
        self,
        *,
        intent_hash: str,
        actor_id: str,
        policy_binding: str,
        authorization_epoch: int,
        now: Optional[float] = None,
    ) -> None:
        """Validate exact intent/actor/policy/epoch bindings under the grant lock."""
        with self._lock:
            self._refresh_state(now)
            if self.state is GrantState.EXPIRED:
                raise GrantClaimDenied("expired")
            if self.state is GrantState.SPENT:
                raise GrantClaimDenied("spent")
            if self.state is GrantState.REVOKED:
                raise GrantClaimDenied("revoked")
            if authorization_epoch != self.authorization_epoch:
                object.__setattr__(self, "state", GrantState.REVOKED)
                raise GrantClaimDenied(
                    "epoch_mismatch",
                    f"grant epoch {self.authorization_epoch} != current {authorization_epoch}",
                )
            if intent_hash != self.intent_hash:
                raise GrantClaimDenied("intent_mismatch")
            if actor_id != self.actor_id:
                raise GrantClaimDenied("actor_mismatch")
            if policy_binding != self.policy_binding:
                raise GrantClaimDenied("policy_mismatch")

    def claim(
        self,
        attempt_id: str,
        *,
        intent_hash: str,
        actor_id: str,
        policy_binding: str,
        authorization_epoch: int,
        now: Optional[float] = None,
    ) -> None:
        """Atomically compare-and-set the one live attempt claim."""
        with self._lock:
            self.validate_live(
                intent_hash=intent_hash,
                actor_id=actor_id,
                policy_binding=policy_binding,
                authorization_epoch=authorization_epoch,
                now=now,
            )
            if self.claimed_by is not None and self.claimed_by != attempt_id:
                raise GrantClaimDenied(
                    "approval_already_claimed",
                    f"claimed by {self.claimed_by!r}",
                )
            if (
                self.claimed_by is None
                and self.precommit_attempts >= self.max_precommit_attempts
            ):
                raise GrantClaimDenied(
                    "attempts_exhausted",
                    f"{self.precommit_attempts} precommit attempts used of "
                    f"{self.max_precommit_attempts}",
                )
            object.__setattr__(self, "claimed_by", attempt_id)

    @contextmanager
    def claim_fence(self, attempt_id: str) -> Iterator[None]:
        """Hold claim ownership stable across the gateway commit protocol."""
        with self._lock:
            if self.claimed_by != attempt_id:
                raise GrantClaimDenied(
                    "claim_not_held",
                    f"claim fence requires {attempt_id!r}, held by {self.claimed_by!r}",
                )
            yield

    def release_claim(self, attempt_id: str) -> None:
        """Release a clean-precommit claim while approval is still ACTIVE."""
        with self._lock:
            if self.state is not GrantState.ACTIVE:
                raise GrantStateError(
                    "release_claim requires ACTIVE approval; spent authority cannot be revived"
                )
            if self.claimed_by != attempt_id:
                raise GrantStateError(
                    f"release_claim called by {attempt_id!r} but claim is held by "
                    f"{self.claimed_by!r}"
                )
            object.__setattr__(self, "claimed_by", None)
            object.__setattr__(self, "precommit_attempts", self.precommit_attempts + 1)

    def spend(self) -> None:
        """ACTIVE -> SPENT. Terminal and irrevocable."""
        with self._lock:
            if self.state is not GrantState.ACTIVE:
                raise GrantStateError(
                    f"spend() requires ACTIVE, got {self.state.value}"
                )
            object.__setattr__(self, "state", GrantState.SPENT)

    def revoke(self) -> None:
        """ACTIVE -> REVOKED. Terminal."""
        with self._lock:
            if self.state is not GrantState.ACTIVE:
                raise GrantStateError(
                    f"revoke() requires ACTIVE, got {self.state.value}"
                )
            object.__setattr__(self, "state", GrantState.REVOKED)


@dataclass
class EffectAttempt:
    """One execution try with sealed fields and method-owned transitions."""

    grant_id: str
    attempt_id: str = field(default_factory=lambda: secrets.token_urlsafe(12))
    effect_id: str = field(default_factory=lambda: secrets.token_urlsafe(16))
    state: AttemptState = AttemptState.PREPARING
    reservation_started: bool = False

    def __setattr__(self, name: str, value: Any) -> None:
        if name in _ATTEMPT_PUBLIC_FIELDS and name in self.__dict__:
            raise GrantStateError(
                f"attempt field {name!r} is read-only; use lifecycle methods"
            )
        object.__setattr__(self, name, value)

    def _require(self, expected: AttemptState) -> None:
        if self.state is not expected:
            raise GrantStateError(
                f"attempt {self.attempt_id!r}: transition requires "
                f"{expected.value}, got {self.state.value}"
            )

    def _require_own_grant(self, grant: ApprovalGrant) -> None:
        if grant.grant_id != self.grant_id:
            raise GrantStateError(
                f"attempt {self.attempt_id!r} belongs to grant "
                f"{self.grant_id!r}, not {grant.grant_id!r}"
            )

    def begin_reservation(self, grant: ApprovalGrant) -> None:
        """Latch that REQUIRED reservation I/O is about to begin.

        Idempotent for retry of the same PREPARING attempt. The latch is set
        before the ledger append because a failed append can still have written
        a durable or partially durable RESERVED fact.
        """
        self._require(AttemptState.PREPARING)
        self._require_own_grant(grant)
        if grant.claimed_by != self.attempt_id:
            raise GrantStateError(
                "begin_reservation requires this attempt to hold the grant claim"
            )
        object.__setattr__(self, "reservation_started", True)

    def mark_no_effect(self, grant: ApprovalGrant) -> None:
        """Clean precommit NO_EFFECT: release claim and preserve approval.

        This path is valid only before REQUIRED reservation I/O starts. Once the
        reservation latch is set, only a gateway-owned durable closure may
        establish NO_EFFECT without risking contradiction with a written row.
        """
        self._require(AttemptState.PREPARING)
        self._require_own_grant(grant)
        if self.reservation_started:
            raise GrantStateError(
                "clean NO_EFFECT is forbidden after reservation I/O has started"
            )
        grant.release_claim(self.attempt_id)
        object.__setattr__(self, "state", AttemptState.NO_EFFECT)

    def mark_no_effect_after_authority(self) -> None:
        """Terminalize proven no-effect after approval authority was spent.

        Used when an issued permit expires unconsumed. No claim is released and
        no precommit retry budget is changed: the approval remains SPENT.
        """
        if self.state not in (AttemptState.PREPARING, AttemptState.RESERVED):
            raise GrantStateError(
                f"attempt {self.attempt_id!r}: post-authority NO_EFFECT requires "
                f"preparing/reserved, got {self.state.value}"
            )
        object.__setattr__(self, "state", AttemptState.NO_EFFECT)

    def mark_reserved(self, grant: ApprovalGrant) -> None:
        """Record that the durable reservation exists; does not spend grant."""
        self._require(AttemptState.PREPARING)
        self._require_own_grant(grant)
        if not self.reservation_started:
            raise GrantStateError(
                "mark_reserved requires begin_reservation before durable append"
            )
        if grant.claimed_by != self.attempt_id:
            raise GrantStateError(
                "mark_reserved requires this attempt to hold the grant claim"
            )
        object.__setattr__(self, "state", AttemptState.RESERVED)

    def mark_effect_confirmed(self) -> None:
        """External evidence establishes the effect."""
        if self.state not in (AttemptState.RESERVED, AttemptState.PREPARING):
            self._require(AttemptState.RESERVED)
        object.__setattr__(self, "state", AttemptState.EFFECT_CONFIRMED)

    def mark_effect_unknown(self) -> None:
        """The effect may have happened; reconciliation is required."""
        if self.state not in (AttemptState.RESERVED, AttemptState.PREPARING):
            self._require(AttemptState.RESERVED)
        object.__setattr__(self, "state", AttemptState.EFFECT_UNKNOWN)


class ApprovalGrantStore:
    """Ephemeral, thread-safe in-memory grant registry."""

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
        self._lock = threading.RLock()

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
        with self._lock:
            self._grants[grant.grant_id] = grant
        return grant

    def get(self, grant_id: str) -> Optional[ApprovalGrant]:
        with self._lock:
            return self._grants.get(grant_id)

    def __len__(self) -> int:
        with self._lock:
            return len(self._grants)
