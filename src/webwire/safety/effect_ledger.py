"""M5 durable effect ledger.

The ledger is safety-critical and intentionally separate from ``journal.ndjson``.
Writes are append-only NDJSON and fsync-backed. A failure is propagated so the
future Commit Gateway can fail closed before a REQUIRED external effect.

Source of truth: docs/M5_DESIGN.md §§10-12.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Optional

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
    """Durable ledger I/O failure. Callers must treat this as fail-closed."""


class EffectLedgerCorruptError(EffectLedgerError):
    """Ledger content cannot be trusted enough to reconstruct safety state."""


class EffectState(StrEnum):
    """Canonical effect-knowledge states from the frozen M5 design."""

    NO_EFFECT = "NO_EFFECT"
    RESERVED = "RESERVED"
    EFFECT_CONFIRMED = "EFFECT_CONFIRMED"
    EFFECT_UNKNOWN = "EFFECT_UNKNOWN"


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
        # A record cannot EXIST invalid — validation runs at construction,
        # not only at parse/serialize boundaries.
        self.validate()

    def validate(self) -> None:
        required = {
            "effect_id": self.effect_id,
            "semantic_key": self.semantic_key,
            "action_type": self.action_type,
            "intent_hash": self.intent_hash,
            "policy_binding": self.policy_binding,
        }
        # Identity fields must be GENUINE non-empty strings. str() coercion at
        # the from_dict boundary would turn a corrupt null into "None" and let
        # it hydrate as a semantic key — exactly the evasion the recovery guard
        # exists to prevent (Codex review, PR #2, 2026-09-23).
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
    """Append-only fsync-backed safety ledger."""

    def __init__(
        self,
        config: Optional[WebWireConfig] = None,
        *,
        path: Optional[Path] = None,
    ) -> None:
        cfg = config or WebWireConfig()
        self._path = path or cfg.effects_path()

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
            # mkdir durability is a parent-directory property. Persist each
            # newly created directory and the entry that names it, top-down.
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

    def append_durable(self, record: EffectLedgerRecord) -> None:
        """Append one record and fsync before returning.

        Any creation/write/fsync failure raises EffectLedgerError. The caller
        must not perform a REQUIRED external effect after such a failure.
        """

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
            raise EffectLedgerError(f"durable effect ledger append failed: {exc!r}") from exc
        finally:
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

        # A new file needs its containing directory entry persisted too.
        if not existed:
            try:
                self._fsync_directory(self._path.parent)
            except OSError as exc:
                raise EffectLedgerError(
                    f"effect ledger directory fsync failed: {exc!r}"
                ) from exc

    def read_records(self) -> list[EffectLedgerRecord]:
        """Read all records in append order.

        Unlike the audit journal, malformed content is not skipped. Losing a
        safety fact could permit replay, so corruption fails closed.
        """

        if not self._path.exists():
            return []
        try:
            lines = self._path.read_text(encoding="utf-8").splitlines()
        except OSError as exc:
            raise EffectLedgerError(f"effect ledger read failed: {exc!r}") from exc

        records: list[EffectLedgerRecord] = []
        identities: dict[str, str] = {}
        for lineno, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise EffectLedgerCorruptError(
                    f"effect ledger line {lineno} is not valid JSON"
                ) from exc
            record = EffectLedgerRecord.from_dict(raw)
            prior_key = identities.setdefault(record.effect_id, record.semantic_key)
            if prior_key != record.semantic_key:
                raise EffectLedgerCorruptError(
                    f"effect_id {record.effect_id!r} changed semantic_key "
                    f"from {prior_key!r} to {record.semantic_key!r}"
                )
            records.append(record)
        return records

    def recovery_projection(self) -> list[RecoveryProjection]:
        """Derive current per-effect recovery truth without rewriting the ledger."""

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
