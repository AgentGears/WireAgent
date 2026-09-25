"""M6 durable evidence-bearing reconciliation ledger.

The reconciliation ledger is safety state, not audit output.  It is deliberately
orthogonal to ``EffectLedger``: M5 effect history remains immutable while M6
records one later operator-authorized reconciliation fact for an unresolved
``effect_id``.

Source of truth: ``docs/M6_DESIGN.md`` §§7-8.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Optional

from webwire.config import WebWireConfig

__all__ = [
    "ReconciliationLedger",
    "ReconciliationLedgerAmbiguousError",
    "ReconciliationLedgerCorruptError",
    "ReconciliationLedgerError",
    "ReconciliationRecord",
    "ReconciliationVerdict",
    "canonical_evidence_hash",
]


class ReconciliationLedgerError(RuntimeError):
    """Durable reconciliation I/O or validity failure; callers fail closed."""


class ReconciliationLedgerCorruptError(ReconciliationLedgerError):
    """Reconciliation content/history cannot be trusted as safety authority."""


class ReconciliationLedgerAmbiguousError(ReconciliationLedgerError):
    """A same-process append may be visible but is not known durable."""


class ReconciliationVerdict(StrEnum):
    """The only terminal M6 reconciliation verdicts."""

    CONFIRMED_EFFECT = "CONFIRMED_EFFECT"
    CONFIRMED_NO_EFFECT = "CONFIRMED_NO_EFFECT"


_REQUIRED_RECORD_FIELDS = frozenset(
    {
        "reconciliation_id",
        "effect_id",
        "semantic_key",
        "action_type",
        "intent_hash",
        "policy_binding",
        "actor_id",
        "target_type",
        "target_id",
        "verdict",
        "operator_id",
        "evidence_hash",
        "evidence",
        "timestamp",
    }
)
_LINEAGE_FIELDS = (
    "semantic_key",
    "action_type",
    "intent_hash",
    "policy_binding",
    "actor_id",
    "target_type",
    "target_id",
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _validate_json_value(value: Any, path: str) -> None:
    """Reject values JSON would coerce or encode non-portably."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(
                    f"{path} keys must be strings, got {type(key).__name__}"
                )
            _validate_json_value(item, f"{path}.{key}")
        return
    raise ValueError(
        f"{path} contains non-JSON value of type {type(value).__name__}"
    )


def _validate_utc_provenance(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty UTC timestamp string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a valid ISO-8601 UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        raise ValueError(f"{field_name} must carry an explicit UTC offset")
    return value


def _canonicalize_evidence(evidence: Any) -> str:
    if not isinstance(evidence, dict) or not evidence:
        raise ValueError("evidence must be a non-empty JSON object")
    _validate_json_value(evidence, "evidence")

    basis = evidence.get("basis")
    if not isinstance(basis, str) or not basis:
        raise ValueError("evidence.basis must be a non-empty string")
    _validate_utc_provenance(evidence.get("observed_at"), "evidence.observed_at")
    observations = evidence.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("evidence.observations must be a non-empty list")
    for index, observation in enumerate(observations):
        if not isinstance(observation, dict) or not observation:
            raise ValueError(
                f"evidence.observations[{index}] must be a non-empty object"
            )

    return json.dumps(
        evidence,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def canonical_evidence_hash(evidence: dict[str, Any]) -> str:
    """Return the normative lowercase SHA-256 of strict canonical evidence JSON."""
    canonical = _canonicalize_evidence(evidence)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, init=False)
class ReconciliationRecord:
    """One immutable M6 terminal reconciliation fact.

    Evidence is stored internally as canonical JSON rather than as the caller's
    mutable ``dict``.  The public ``evidence`` property returns a fresh decoded
    object, so post-construction caller mutation cannot alter the fact that was
    validated and hashed.
    """

    reconciliation_id: str
    effect_id: str
    semantic_key: str
    action_type: str
    intent_hash: str
    policy_binding: str
    actor_id: Optional[str]
    target_type: Optional[str]
    target_id: Optional[str]
    verdict: ReconciliationVerdict
    operator_id: str
    evidence_hash: str
    timestamp: str
    _evidence_json: str = field(repr=False)

    def __init__(
        self,
        *,
        reconciliation_id: str,
        effect_id: str,
        semantic_key: str,
        action_type: str,
        intent_hash: str,
        policy_binding: str,
        verdict: ReconciliationVerdict,
        operator_id: str,
        evidence_hash: str,
        evidence: dict[str, Any],
        actor_id: Optional[str] = None,
        target_type: Optional[str] = None,
        target_id: Optional[str] = None,
        timestamp: Optional[str] = None,
    ) -> None:
        required_strings: dict[str, Any] = {
            "reconciliation_id": reconciliation_id,
            "effect_id": effect_id,
            "semantic_key": semantic_key,
            "action_type": action_type,
            "intent_hash": intent_hash,
            "policy_binding": policy_binding,
            "operator_id": operator_id,
        }
        for name, value in required_strings.items():
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name} must be a non-empty string")

        optional_strings: dict[str, Optional[str]] = {
            "actor_id": actor_id,
            "target_type": target_type,
            "target_id": target_id,
        }
        for name, value in optional_strings.items():
            if value is not None and (not isinstance(value, str) or not value):
                raise ValueError(f"{name} must be None or a non-empty string")

        if not isinstance(verdict, ReconciliationVerdict):
            raise ValueError("verdict must be a ReconciliationVerdict")
        if not isinstance(evidence_hash, str) or _SHA256_RE.fullmatch(evidence_hash) is None:
            raise ValueError("evidence_hash must be lowercase SHA-256 hexadecimal")

        canonical_evidence = _canonicalize_evidence(evidence)
        actual_hash = hashlib.sha256(canonical_evidence.encode("utf-8")).hexdigest()
        if evidence_hash != actual_hash:
            raise ValueError("evidence_hash does not match canonical evidence JSON")

        actual_timestamp = timestamp or datetime.now(timezone.utc).isoformat()
        _validate_utc_provenance(actual_timestamp, "timestamp")

        object.__setattr__(self, "reconciliation_id", reconciliation_id)
        object.__setattr__(self, "effect_id", effect_id)
        object.__setattr__(self, "semantic_key", semantic_key)
        object.__setattr__(self, "action_type", action_type)
        object.__setattr__(self, "intent_hash", intent_hash)
        object.__setattr__(self, "policy_binding", policy_binding)
        object.__setattr__(self, "actor_id", actor_id)
        object.__setattr__(self, "target_type", target_type)
        object.__setattr__(self, "target_id", target_id)
        object.__setattr__(self, "verdict", verdict)
        object.__setattr__(self, "operator_id", operator_id)
        object.__setattr__(self, "evidence_hash", evidence_hash)
        object.__setattr__(self, "timestamp", actual_timestamp)
        object.__setattr__(self, "_evidence_json", canonical_evidence)

    @property
    def evidence(self) -> dict[str, Any]:
        """Return a detached evidence object; mutating it cannot alter this fact."""
        decoded = json.loads(self._evidence_json)
        if not isinstance(decoded, dict):  # pragma: no cover - construction guarantees it
            raise AssertionError("canonical evidence is not an object")
        return decoded

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciliation_id": self.reconciliation_id,
            "effect_id": self.effect_id,
            "semantic_key": self.semantic_key,
            "action_type": self.action_type,
            "intent_hash": self.intent_hash,
            "policy_binding": self.policy_binding,
            "actor_id": self.actor_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "verdict": self.verdict.value,
            "operator_id": self.operator_id,
            "evidence_hash": self.evidence_hash,
            "evidence": self.evidence,
            "timestamp": self.timestamp,
        }

    def to_jsonl(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @classmethod
    def from_dict(cls, raw: Any) -> "ReconciliationRecord":
        try:
            if not isinstance(raw, dict):
                raise ValueError("record must be a JSON object")
            keys = frozenset(raw.keys())
            if keys != _REQUIRED_RECORD_FIELDS:
                missing = sorted(_REQUIRED_RECORD_FIELDS - keys)
                extra = sorted(keys - _REQUIRED_RECORD_FIELDS)
                raise ValueError(
                    f"record schema mismatch; missing={missing!r} extra={extra!r}"
                )
            evidence = raw["evidence"]
            if not isinstance(evidence, dict):
                raise ValueError("evidence must be an object")
            return cls(
                reconciliation_id=raw["reconciliation_id"],
                effect_id=raw["effect_id"],
                semantic_key=raw["semantic_key"],
                action_type=raw["action_type"],
                intent_hash=raw["intent_hash"],
                policy_binding=raw["policy_binding"],
                actor_id=raw["actor_id"],
                target_type=raw["target_type"],
                target_id=raw["target_id"],
                verdict=ReconciliationVerdict(raw["verdict"]),
                operator_id=raw["operator_id"],
                evidence_hash=raw["evidence_hash"],
                evidence=evidence,
                timestamp=raw["timestamp"],
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReconciliationLedgerCorruptError(
                f"invalid reconciliation ledger record: {exc}"
            ) from exc


@dataclass
class _PathState:
    lock: Any
    ambiguous_fact: Optional[ReconciliationRecord] = None


class ReconciliationLedger:
    """Append-only fsync-backed M6 reconciliation safety ledger.

    All objects for one normalized path share a process-local writer lock and
    durability-ambiguity latch.  The latch is intentionally stronger than M5's
    exact-fact retry behavior: while set, ordinary reads and non-exact writes
    fail closed until the exact visible fact is re-durabilized.
    """

    _states_guard: ClassVar[Any] = threading.Lock()
    _states: ClassVar[dict[str, _PathState]] = {}

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        *,
        path: Optional[Path] = None,
    ) -> None:
        cfg = config or WebWireConfig()
        selected = path or cfg.reconciliations_path()
        self._path = selected.resolve(strict=False)
        self._path_state = self._state_for_path(self._path)
        self._lock = self._path_state.lock

    @classmethod
    def _state_for_path(cls, path: Path) -> _PathState:
        key = os.path.normcase(str(path.resolve(strict=False)))
        with cls._states_guard:
            state = cls._states.get(key)
            if state is None:
                state = _PathState(lock=threading.RLock())
                cls._states[key] = state
            return state

    @property
    def path(self) -> Path:
        return self._path

    @property
    def durability_ambiguous(self) -> bool:
        with self._lock:
            return self._path_state.ambiguous_fact is not None

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        fd: Optional[int] = None
        try:
            fd = os.open(path, os.O_RDONLY)
            os.fsync(fd)
        finally:
            if fd is not None:
                os.close(fd)

    def _ensure_parent(self) -> None:
        missing: list[Path] = []
        cursor = self._path.parent
        while not cursor.exists():
            missing.append(cursor)
            parent = cursor.parent
            if parent == cursor:
                break
            cursor = parent
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            for created in reversed(missing):
                self._fsync_directory(created)
                self._fsync_directory(created.parent)
        except OSError as exc:
            raise ReconciliationLedgerError(
                f"could not durably create reconciliation ledger directory "
                f"{self._path.parent}: {exc!r}"
            ) from exc

    @staticmethod
    def _same_fact(
        existing: ReconciliationRecord,
        proposed: ReconciliationRecord,
    ) -> bool:
        """Exact immutable fact equality, excluding wall-clock provenance time."""
        return (
            existing.reconciliation_id == proposed.reconciliation_id
            and existing.effect_id == proposed.effect_id
            and all(
                getattr(existing, name) == getattr(proposed, name)
                for name in _LINEAGE_FIELDS
            )
            and existing.verdict is proposed.verdict
            and existing.operator_id == proposed.operator_id
            and existing.evidence_hash == proposed.evidence_hash
            and existing._evidence_json == proposed._evidence_json
        )

    @staticmethod
    def _validate_history(records: list[ReconciliationRecord]) -> None:
        by_id: dict[str, ReconciliationRecord] = {}
        by_effect: dict[str, ReconciliationRecord] = {}
        for record in records:
            prior_id = by_id.get(record.reconciliation_id)
            if prior_id is not None:
                raise ReconciliationLedgerCorruptError(
                    f"reconciliation_id {record.reconciliation_id!r} appears more than once"
                )
            prior_effect = by_effect.get(record.effect_id)
            if prior_effect is not None:
                raise ReconciliationLedgerCorruptError(
                    f"effect_id {record.effect_id!r} has more than one terminal reconciliation"
                )
            by_id[record.reconciliation_id] = record
            by_effect[record.effect_id] = record

    def _read_records_unchecked(self) -> list[ReconciliationRecord]:
        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise ReconciliationLedgerError(
                f"reconciliation ledger read failed: {exc!r}"
            ) from exc

        records: list[ReconciliationRecord] = []
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReconciliationLedgerCorruptError(
                    f"reconciliation ledger line {lineno} is not valid JSON"
                ) from exc
            records.append(ReconciliationRecord.from_dict(raw))
        self._validate_history(records)
        return records

    def _redurable_existing_file(self) -> None:
        fd: Optional[int] = None
        try:
            fd = os.open(self._path, os.O_RDWR)
            os.fsync(fd)
            self._fsync_directory(self._path.parent)
        except OSError as exc:
            raise ReconciliationLedgerError(
                f"reconciliation ledger re-durability fsync failed: {exc!r}"
            ) from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def read_records(self) -> list[ReconciliationRecord]:
        """Read valid records only when current-process durability is unambiguous."""
        with self._lock:
            if self._path_state.ambiguous_fact is not None:
                raise ReconciliationLedgerAmbiguousError(
                    "reconciliation ledger durability is ambiguous; exact-fact "
                    "re-durability is required"
                )
            return self._read_records_unchecked()

    def read_authoritative(self) -> list[ReconciliationRecord]:
        """Read startup/enforcement authority after re-establishing file durability.

        A missing file is valid empty history.  A current-process ambiguity latch
        is *not* cleared by this method; only exact-fact retry may do that.  After
        process restart the old latch no longer exists, so this writable-handle
        fsync establishes current durability before surviving rows become
        authority.
        """
        with self._lock:
            if self._path_state.ambiguous_fact is not None:
                raise ReconciliationLedgerAmbiguousError(
                    "reconciliation ledger durability is ambiguous; exact-fact "
                    "re-durability is required"
                )
            records = self._read_records_unchecked()
            if not self._path.exists():
                return records
            self._redurable_existing_file()
            return records

    def append_durable(self, record: ReconciliationRecord) -> None:
        """Validate, append and fsync one terminal reconciliation fact."""
        with self._lock:
            if not isinstance(record, ReconciliationRecord):
                raise ValueError("record must be a ReconciliationRecord")

            ambiguous = self._path_state.ambiguous_fact
            if ambiguous is not None:
                if not self._same_fact(ambiguous, record):
                    raise ReconciliationLedgerAmbiguousError(
                        "reconciliation ledger durability is ambiguous for a "
                        "different fact"
                    )
                visible = self._read_records_unchecked()
                matching = [
                    prior for prior in visible if self._same_fact(prior, record)
                ]
                if len(matching) != 1:
                    raise ReconciliationLedgerAmbiguousError(
                        "ambiguous reconciliation fact is not present as one "
                        "complete exact visible row"
                    )
                try:
                    self._redurable_existing_file()
                except ReconciliationLedgerError:
                    raise
                self._path_state.ambiguous_fact = None
                return

            existing = self._read_records_unchecked()
            for prior in existing:
                if prior.reconciliation_id == record.reconciliation_id:
                    if self._same_fact(prior, record):
                        try:
                            self._redurable_existing_file()
                        except ReconciliationLedgerError:
                            self._path_state.ambiguous_fact = record
                            raise
                        return
                    raise ReconciliationLedgerCorruptError(
                        f"reconciliation_id {record.reconciliation_id!r} collides "
                        "with a different fact"
                    )
                if prior.effect_id == record.effect_id:
                    raise ReconciliationLedgerCorruptError(
                        f"effect_id {record.effect_id!r} already has a terminal "
                        "reconciliation"
                    )

            self._validate_history([*existing, record])
            payload = (record.to_jsonl() + "\n").encode("utf-8")
            self._ensure_parent()
            existed = self._path.exists()

            fd: Optional[int] = None
            total = 0
            try:
                fd = os.open(
                    self._path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                view = memoryview(payload)
                while total < len(payload):
                    written = os.write(fd, view[total:])
                    if written <= 0:
                        raise OSError("reconciliation ledger write returned no progress")
                    total += written
                os.fsync(fd)
            except OSError as exc:
                if total > 0:
                    self._path_state.ambiguous_fact = record
                raise ReconciliationLedgerError(
                    f"durable reconciliation ledger append failed: {exc!r}"
                ) from exc
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass

            if not existed:
                try:
                    self._fsync_directory(self._path.parent)
                except OSError as exc:
                    self._path_state.ambiguous_fact = record
                    raise ReconciliationLedgerError(
                        f"reconciliation ledger directory fsync failed: {exc!r}"
                    ) from exc
