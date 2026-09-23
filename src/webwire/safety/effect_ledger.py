"""M5 durable effect ledger.

The ledger is safety-critical and intentionally separate from ``journal.ndjson``.
Writes are append-only NDJSON and fsync-backed.  A failure is propagated so the
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

    def validate(self) -> None:
        required = {
            "effect_id": self.effect_id,
            "semantic_key": self.semantic_key,
            "action_type": self.action_type,
            "intent_hash": self.intent_hash,
            "policy_binding": self.policy_binding,
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "effect ledger record missing required fields: " + ", ".join(missing)
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
            record = cls(
                effect_id=str(raw["effect_id"]),
                semantic_key=str(raw["semantic_key"]),
                state=state,
                action_type=str(raw["action_type"]),
                intent_hash=str(raw["intent_hash"]),
                policy_binding=str(raw["policy_binding"]),
                actor_id=raw.get("actor_id"),
                target_type=raw.get("target_type"),
                target_id=raw.get("target_id"),
                timestamp=str(raw["timestamp"]),
                details=dict(raw.get("details") or {}),
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

    def _ensure_parent(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise EffectLedgerError(
                f"could not create effect ledger directory {self._path.parent}: {exc!r}"
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

        Any creation/write/fsync failure raises EffectLedgerError.  The caller
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

        # If this append created the file, persist the directory entry too.
        if not existed:
            dir_fd: Optional[int] = None
            try:
                dir_fd = os.open(self._path.parent, os.O_RDONLY)
                os.fsync(dir_fd)
            except OSError as exc:
                raise EffectLedgerError(
                    f"effect ledger directory fsync failed: {exc!r}"
                ) from exc
            finally:
                if dir_fd is not None:
                    try:
                        os.close(dir_fd)
                    except OSError:
                        pass

    def read_records(self) -> list[EffectLedgerRecord]:
        """Read all records in append order.

        Unlike the audit journal, malformed content is not skipped.  Losing a
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
