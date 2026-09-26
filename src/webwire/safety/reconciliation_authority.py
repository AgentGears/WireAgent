"""M6 ephemeral operator authority for one terminal reconciliation fact.

ReconciliationAuthority is process-local safety authority. It is bound to one
(effect, verdict, evidence hash, operator) tuple, uses monotonic elapsed time
before persistence starts, and can never mint ordinary execution authority.

Once the coordinator starts persistence it commits this authority to one exact
frozen ReconciliationRecord. TTL expiry after that point cannot change or
cancel the already-approved fact; only exact-fact re-drive/re-durability is
allowed until known durable success or process termination.

Lifecycle transitions are additionally bound to the coordinator that minted the
authority. The protocol key is deliberately private to that coordinator: callers
may inspect the returned authority, but ordinary code cannot synthetically mark
it committed/consumed or manufacture a separately constructed authority that a
different coordinator will accept. As elsewhere in the safety models this is an
engineering boundary against accidental/API-level bypass, not a sandbox against
hostile Python reflection.

Source of truth: ``docs/M6_DESIGN.md`` §§10-12 and invariants 11-14.
"""

from __future__ import annotations

import math
import secrets
import threading
import time
from collections.abc import Callable
from typing import Optional

from webwire.safety.reconciliation_ledger import (
    ReconciliationRecord,
    ReconciliationVerdict,
)

__all__ = [
    "DEFAULT_RECONCILIATION_AUTHORITY_TTL_S",
    "ReconciliationAuthority",
    "ReconciliationAuthorityError",
]

DEFAULT_RECONCILIATION_AUTHORITY_TTL_S = 120.0


class ReconciliationAuthorityError(RuntimeError):
    """Reconciliation operator authority is absent, stale, or inconsistent."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(
            f"reconciliation authority denied: {reason}"
            + (f" — {detail}" if detail else "")
        )


class ReconciliationAuthority:
    """One process-local, monotonic-TTL authority bound to one resolution.

    Public properties are diagnostic carrier data only. Commitment and
    consumption state remain private and synchronized by ``_lock``. Lifecycle
    methods are coordinator-internal and require the exact protocol key supplied
    when the authority was minted.
    """

    def __init__(
        self,
        *,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence_hash: str,
        operator_id: str,
        _protocol_key: object,
        ttl_seconds: float = DEFAULT_RECONCILIATION_AUTHORITY_TTL_S,
        monotonic_clock: Callable[[], float] = time.monotonic,
        authority_id_factory: Callable[[], str] = lambda: secrets.token_urlsafe(16),
    ) -> None:
        if not isinstance(effect_id, str) or not effect_id:
            raise ValueError("effect_id must be a non-empty string")
        if not isinstance(verdict, ReconciliationVerdict):
            raise ValueError("verdict must be a ReconciliationVerdict")
        if not isinstance(evidence_hash, str) or len(evidence_hash) != 64:
            raise ValueError("evidence_hash must be lowercase SHA-256 hexadecimal")
        try:
            int(evidence_hash, 16)
        except ValueError as exc:
            raise ValueError(
                "evidence_hash must be lowercase SHA-256 hexadecimal"
            ) from exc
        if evidence_hash.lower() != evidence_hash:
            raise ValueError("evidence_hash must be lowercase SHA-256 hexadecimal")
        if not isinstance(operator_id, str) or not operator_id:
            raise ValueError("operator_id must be a non-empty string")
        if _protocol_key is None:
            raise ValueError("_protocol_key is required")
        try:
            ttl = float(ttl_seconds)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("ttl_seconds must be a finite positive number") from exc
        if not math.isfinite(ttl) or ttl <= 0:
            raise ValueError("ttl_seconds must be a finite positive number")

        self._effect_id = effect_id
        self._verdict = verdict
        self._evidence_hash = evidence_hash
        self._operator_id = operator_id
        self.__protocol_key = _protocol_key
        self._clock = monotonic_clock
        self._lock = threading.RLock()
        self._last_clock_sample: Optional[float] = None
        self._committed_record: Optional[ReconciliationRecord] = None
        self._consumed = False

        with self._lock:
            issued_at = self._sample_clock_locked()
            expires_at = issued_at + ttl
            if not math.isfinite(expires_at):
                raise ValueError("reconciliation authority deadline is non-finite")
            authority_id = authority_id_factory()
            if not isinstance(authority_id, str) or not authority_id:
                raise ValueError("authority_id_factory must return a non-empty string")
            self._authority_id = authority_id
            self._issued_at = issued_at
            self._expires_at = expires_at

    @property
    def authority_id(self) -> str:
        return self._authority_id

    @property
    def effect_id(self) -> str:
        return self._effect_id

    @property
    def verdict(self) -> ReconciliationVerdict:
        return self._verdict

    @property
    def evidence_hash(self) -> str:
        return self._evidence_hash

    @property
    def operator_id(self) -> str:
        return self._operator_id

    @property
    def issued_at(self) -> float:
        """Process-relative diagnostic value; never persist across restart."""
        return self._issued_at

    @property
    def expires_at(self) -> float:
        """Process-relative diagnostic value; meaningful only before commit."""
        return self._expires_at

    @property
    def committed(self) -> bool:
        with self._lock:
            return self._committed_record is not None

    @property
    def consumed(self) -> bool:
        with self._lock:
            return self._consumed

    @property
    def committed_record(self) -> Optional[ReconciliationRecord]:
        """Return the immutable frozen fact, if persistence has started."""
        with self._lock:
            return self._committed_record

    def _sample_clock_locked(self) -> float:
        try:
            value = float(self._clock())
        except (TypeError, ValueError, OverflowError) as exc:
            raise ReconciliationAuthorityError(
                "authority_clock_invalid",
                "monotonic clock returned an invalid value",
            ) from exc
        if not math.isfinite(value):
            raise ReconciliationAuthorityError(
                "authority_clock_invalid",
                "monotonic clock returned a non-finite value",
            )
        previous = self._last_clock_sample
        if previous is not None and value < previous:
            raise ReconciliationAuthorityError(
                "authority_clock_regressed",
                "monotonic authority clock regressed",
            )
        self._last_clock_sample = value
        return value

    def _require_protocol_key(self, protocol_key: object) -> None:
        if protocol_key is not self.__protocol_key:
            raise ReconciliationAuthorityError("authority_protocol_mismatch")

    def _require_binding(
        self,
        *,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence_hash: str,
    ) -> None:
        if effect_id != self._effect_id:
            raise ReconciliationAuthorityError("effect_mismatch")
        if verdict is not self._verdict:
            raise ReconciliationAuthorityError("verdict_mismatch")
        if evidence_hash != self._evidence_hash:
            raise ReconciliationAuthorityError("evidence_hash_mismatch")

    def _validate_start(
        self,
        *,
        protocol_key: object,
        effect_id: str,
        verdict: ReconciliationVerdict,
        evidence_hash: str,
    ) -> Optional[ReconciliationRecord]:
        """Coordinator-internal validation for one persistence attempt.

        Returns the exact frozen record when persistence had already started.
        In that committed state TTL is deliberately no longer consulted; the
        caller may only continue that same immutable fact.
        """
        with self._lock:
            self._require_protocol_key(protocol_key)
            self._require_binding(
                effect_id=effect_id,
                verdict=verdict,
                evidence_hash=evidence_hash,
            )
            if self._consumed:
                raise ReconciliationAuthorityError("authority_consumed")
            if self._committed_record is not None:
                return self._committed_record
            now = self._sample_clock_locked()
            if now >= self._expires_at:
                raise ReconciliationAuthorityError("authority_expired")
            return None

    @staticmethod
    def _exact_record(
        left: ReconciliationRecord,
        right: ReconciliationRecord,
    ) -> bool:
        # Commitment freezes the complete record, including timestamp. The
        # ledger's timestamp-excluding exact-re-durability comparison is a lower
        # storage-layer compatibility rule and must not weaken authority binding.
        return left.to_jsonl() == right.to_jsonl()

    def _commit_for_persistence(
        self,
        record: ReconciliationRecord,
        *,
        protocol_key: object,
    ) -> ReconciliationRecord:
        """Coordinator-internal commitment immediately before durability I/O."""
        if not isinstance(record, ReconciliationRecord):
            raise TypeError("record must be a ReconciliationRecord")
        with self._lock:
            self._require_protocol_key(protocol_key)
            self._require_binding(
                effect_id=record.effect_id,
                verdict=record.verdict,
                evidence_hash=record.evidence_hash,
            )
            if record.operator_id != self._operator_id:
                raise ReconciliationAuthorityError("operator_mismatch")
            if self._consumed:
                raise ReconciliationAuthorityError("authority_consumed")
            if self._committed_record is None:
                self._committed_record = record
                return record
            if not self._exact_record(self._committed_record, record):
                raise ReconciliationAuthorityError("committed_fact_mismatch")
            return self._committed_record

    def _consume_after_durable(
        self,
        record: ReconciliationRecord,
        *,
        protocol_key: object,
    ) -> None:
        """Coordinator-internal permanent consume after known durable success."""
        if not isinstance(record, ReconciliationRecord):
            raise TypeError("record must be a ReconciliationRecord")
        with self._lock:
            self._require_protocol_key(protocol_key)
            committed = self._committed_record
            if committed is None:
                raise ReconciliationAuthorityError("authority_not_committed")
            if not self._exact_record(committed, record):
                raise ReconciliationAuthorityError("committed_fact_mismatch")
            if self._consumed:
                raise ReconciliationAuthorityError("authority_consumed")
            self._consumed = True
