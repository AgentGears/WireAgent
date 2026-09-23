"""M5 durable effect ledger.

The ledger is safety-critical and intentionally separate from ``journal.ndjson``.
Writes are append-only NDJSON and fsync-backed. A failure is propagated so the
Commit Gateway can fail closed before a REQUIRED external effect.

The ledger owns not just record syntax but canonical history semantics. For one
``effect_id`` the semantic lineage is immutable and effect-knowledge state may
only move forward. Recovery therefore never accepts a contradictory "last row
wins" history as authority.

Source of truth: docs/M5_DESIGN.md §§10-12.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, ClassVar, Optional

from webwire.config import WebWireConfig

__all__ = [
    "EffectLedger",
    "EffectLedgerCorruptError",
    "EffectLedgerError",
    "EffectLedgerRecord",
    "EffectState",
    "RecoveryProjection",
]


class EffectLedgerError(RuntimeError):
    """Durable ledger I/O or validity failure. Callers must fail closed."""


class EffectLedgerCorruptError(EffectLedgerError):
    """Ledger content/history cannot be trusted as safety authority."""


class EffectState(StrEnum):
    """Canonical effect-knowledge states from the M5 design."""

    NO_EFFECT = "NO_EFFECT"
    RESERVED = "RESERVED"
    EFFECT_CONFIRMED = "EFFECT_CONFIRMED"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


# A fenced effect begins with RESERVED. A replay-safe BEST_EFFORT effect has no
# precommit reservation and can first appear only when its post-boundary outcome
# is known/unknown. NO_EFFECT is therefore never a first durable fact in layer 3;
# it closes an unused RESERVED permit.
_INITIAL_STATES = frozenset(
    {
        EffectState.RESERVED,
        EffectState.EFFECT_CONFIRMED,
        EffectState.EFFECT_UNKNOWN,
    }
)
_ALLOWED_SUCCESSORS: dict[EffectState, frozenset[EffectState]] = {
    EffectState.RESERVED: frozenset(
        {
            EffectState.NO_EFFECT,
            EffectState.EFFECT_CONFIRMED,
            EffectState.EFFECT_UNKNOWN,
        }
    ),
    EffectState.NO_EFFECT: frozenset(),
    EffectState.EFFECT_CONFIRMED: frozenset(),
    EffectState.EFFECT_UNKNOWN: frozenset(),
}
_LINEAGE_FIELDS = (
    "semantic_key",
    "action_type",
    "intent_hash",
    "policy_binding",
    "actor_id",
    "target_type",
    "target_id",
)


@dataclass(frozen=True)
class EffectLedgerRecord:
    """One append-only effect-state fact."""

    effect_id: str
    semantic_key: str
    state: EffectState
    action_type: str
    intent_hash: str
    policy_binding: str
    actor_id: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        required = {
            "effect_id": self.effect_id,
            "semantic_key": self.semantic_key,
            "action_type": self.action_type,
            "intent_hash": self.intent_hash,
            "policy_binding": self.policy_binding,
        }
        for name, value in required.items():
            if not isinstance(value, str) or not value:
                raise ValueError(
                    f"effect ledger record field {name!r} must be a non-empty "
                    f"string, got {type(value).__name__}"
                )

    def to_jsonl(self) -> str:
        self.validate()
        payload = asdict(self)
        payload["state"] = self.state.value
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "EffectLedgerRecord":
        try:
            state = EffectState(raw["state"])
            details_raw = raw.get("details") or {}
            if not isinstance(details_raw, dict):
                raise ValueError("details must be an object when present")
            record = cls(
                effect_id=raw["effect_id"],
                semantic_key=raw["semantic_key"],
                state=state,
                action_type=raw["action_type"],
                intent_hash=raw["intent_hash"],
                policy_binding=raw["policy_binding"],
                actor_id=raw.get("actor_id"),
                target_type=raw.get("target_type"),
                target_id=raw.get("target_id"),
                timestamp=raw["timestamp"],
                details=details_raw,
            )
            record.validate()
            return record
        except (KeyError, TypeError, ValueError) as exc:
            raise EffectLedgerCorruptError(f"invalid effect ledger record: {exc}") from exc


@dataclass(frozen=True)
class RecoveryProjection:
    """Derived recovery state; raw ledger records are never rewritten."""

    effect_id: str
    semantic_key: str
    raw_state: EffectState
    effective_state: EffectState
    unresolved: bool
    last_record: EffectLedgerRecord


class EffectLedger:
    """Append-only fsync-backed safety ledger.

    All instances targeting the same normalized path share one process-local
    re-entrant lock. That makes history-validation + append one critical section
    even if the runtime constructs more than one ``EffectLedger`` object. M5 is
    a single-process runtime; coordinating independent external writers is not a
    property of this file format.
    """

    _path_locks_guard: ClassVar[Any] = threading.Lock()
    _path_locks: ClassVar[dict[str, Any]] = {}

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        *,
        path: Optional[Path] = None,
    ) -> None:
        cfg = config or WebWireConfig()
        self._path = path or cfg.effects_path()
        self._lock = self._lock_for_path(self._path)

    @classmethod
    def _lock_for_path(cls, path: Path) -> Any:
        key = str(path.resolve(strict=False))
        with cls._path_locks_guard:
            lock = cls._path_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                cls._path_locks[key] = lock
            return lock

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        """Persist directory metadata where the platform exposes that primitive.

        POSIX requires the parent directory to be fsync'd for a newly created
        child entry to be crash-durable. Python does not expose a portable
        directory FlushFileBuffers equivalent on Windows, so NT relies on the
        file-handle fsync while preserving the same fail-closed file contract.
        """

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
            raise EffectLedgerError(
                f"could not durably create effect ledger directory "
                f"{self._path.parent}: {exc!r}"
            ) from exc

    @staticmethod
    def _write_all(fd: int, payload: bytes) -> None:
        view = memoryview(payload)
        total = 0
        while total < len(payload):
            written = os.write(fd, view[total:])
            if written <= 0:
                raise OSError("effect ledger write returned no progress")
            total += written

    @staticmethod
    def _validate_history(records: list[EffectLedgerRecord]) -> None:
        """Validate immutable lineage and the canonical per-effect state machine."""
        first_by_effect: dict[str, EffectLedgerRecord] = {}
        last_by_effect: dict[str, EffectLedgerRecord] = {}

        for record in records:
            first = first_by_effect.get(record.effect_id)
            if first is None:
                if record.state not in _INITIAL_STATES:
                    raise EffectLedgerCorruptError(
                        f"effect_id {record.effect_id!r} has illegal initial state "
                        f"{record.state.value}"
                    )
                first_by_effect[record.effect_id] = record
                last_by_effect[record.effect_id] = record
                continue

            for field_name in _LINEAGE_FIELDS:
                original = getattr(first, field_name)
                current = getattr(record, field_name)
                if current != original:
                    raise EffectLedgerCorruptError(
                        f"effect_id {record.effect_id!r} changed {field_name} "
                        f"from {original!r} to {current!r}"
                    )

            previous = last_by_effect[record.effect_id]
            allowed = _ALLOWED_SUCCESSORS[previous.state]
            if record.state not in allowed:
                raise EffectLedgerCorruptError(
                    f"effect_id {record.effect_id!r} has illegal transition "
                    f"{previous.state.value} -> {record.state.value}"
                )
            last_by_effect[record.effect_id] = record

    def append_durable(self, record: EffectLedgerRecord) -> None:
        """Validate, append, and fsync one canonical effect fact.

        Any history/creation/write/fsync failure raises EffectLedgerError. The
        caller must not perform a REQUIRED external effect after such a failure.
        """
        with self._lock:
            record.validate()
            existing = self.read_records()
            self._validate_history([*existing, record])

            payload = (record.to_jsonl() + "\n").encode("utf-8")
            self._ensure_parent()
            existed = self._path.exists()
            fd: Optional[int] = None
            try:
                fd = os.open(
                    self._path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o600,
                )
                self._write_all(fd, payload)
                os.fsync(fd)
            except OSError as exc:
                raise EffectLedgerError(
                    f"durable effect ledger append failed: {exc!r}"
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
                    raise EffectLedgerError(
                        f"effect ledger directory fsync failed: {exc!r}"
                    ) from exc

    def read_records(self) -> list[EffectLedgerRecord]:
        """Read and validate all records in append order.

        Unlike the audit journal, malformed content and impossible histories
        are not skipped. Losing or contradicting a safety fact could permit
        replay, so corruption fails closed.
        """
        with self._lock:
            if not self._path.exists():
                return []
            try:
                lines = self._path.read_text(encoding="utf-8").splitlines()
            except OSError as exc:
                raise EffectLedgerError(
                    f"effect ledger read failed: {exc!r}"
                ) from exc

            records: list[EffectLedgerRecord] = []
            for lineno, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise EffectLedgerCorruptError(
                        f"effect ledger line {lineno} is not valid JSON"
                    ) from exc
                records.append(EffectLedgerRecord.from_dict(raw))

            self._validate_history(records)
            return records

    def recovery_projection(self) -> list[RecoveryProjection]:
        """Derive current per-effect recovery truth without rewriting evidence."""

        latest: dict[str, EffectLedgerRecord] = {}
        for record in self.read_records():
            latest[record.effect_id] = record

        projected: list[RecoveryProjection] = []
        for effect_id, record in latest.items():
            unresolved = record.state in {
                EffectState.RESERVED,
                EffectState.EFFECT_UNKNOWN,
            }
            effective = (
                EffectState.EFFECT_UNKNOWN
                if record.state == EffectState.RESERVED
                else record.state
            )
            projected.append(
                RecoveryProjection(
                    effect_id=effect_id,
                    semantic_key=record.semantic_key,
                    raw_state=record.state,
                    effective_state=effective,
                    unresolved=unresolved,
                    last_record=record,
                )
            )
        return sorted(projected, key=lambda item: item.effect_id)

    def unresolved_semantic_keys(self) -> set[str]:
        """Keys a future RecoveryGuard must deny before browser mutation."""

        return {
            item.semantic_key
            for item in self.recovery_projection()
            if item.unresolved
        }
