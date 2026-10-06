"""M7 Layer 4 — the IPC wire protocol: schemas, canonicalization, identity.

One strict, non-executable protocol over bounded UTF-8 JSON (frozen
§16–§10.5). This module owns:

- the frozen frame/request/response size limits (centralized constants —
  no unbounded read, queue, or allocation anywhere in the IPC path);
- the handshake model (``AuthorityHello``): exact protocol version,
  diagnostic runtime version, EXACT runtime_build_id, the owner's
  authority_instance_id, canonical absolute domain, the five supported
  operations, and lifecycle state;
- deterministic runtime build identity (artifact/source hashing — package
  version is NOT a build identity; an unavailable/ambiguous exact
  identity means production IPC does not enter READY);
- strict post-schema request validation and normalization for exactly the
  five Layer-4 operations, deliberately NARROWER than the permissive
  aliases local capabilities accept;
- canonical request identity: sorted-key deterministic serialization of
  protocol_version + operation + normalized payload — request_id,
  runtime_build_id, and authority_instance_id are excluded by §10.5.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

__all__ = [
    "IPC_PROTOCOL_VERSION",
    "IPC_SUPPORTED_OPERATIONS",
    "IPC_MAX_FRAME_BYTES",
    "IPC_MAX_REQUEST_BYTES",
    "IPC_MAX_RESPONSE_BYTES",
    "IPC_MAX_STRING_BYTES",
    "IPC_MAX_LIMIT",
    "IPC_READ_TABS",
    "IPC_SEARCH_TABS",
    "IPC_SCHEMA",
    "IPCProtocolError",
    "IPCSchemaError",
    "IPCBuildIdentityError",
    "AuthorityHello",
    "compute_runtime_build_id",
    "validate_and_normalize_request",
    "canonical_request_identity",
]

# -- frozen constants (centralized; no scattered magic numbers) --------------

IPC_PROTOCOL_VERSION = 1
IPC_SUPPORTED_OPERATIONS = frozenset({"health", "read", "read_profile", "read_thread", "read_search"})
IPC_MAX_FRAME_BYTES = 1 << 20  # 1 MiB — a frame header + payload ceiling
IPC_MAX_REQUEST_BYTES = 64 * 1024  # 64 KiB — a single request payload
IPC_MAX_RESPONSE_BYTES = 1 << 20  # 1 MiB — a single response payload
IPC_MAX_STRING_BYTES = 2048  # any single string field
IPC_MAX_LIMIT = 100  # read limits cannot exceed this

IPC_READ_TABS = frozenset({"posts", "replies", "media"})  # exactly the capability's enum (F-65: no superset)
IPC_SEARCH_TABS = frozenset({"top", "latest", "people"})


class IPCProtocolError(ValueError):
    """Wire-level protocol violation (framing, handshake, version)."""


class IPCSchemaError(ValueError):
    """Request schema violation: unknown fields, wrong types, invalid
    enums, non-finite numbers, or unreasonable sizes."""


class IPCBuildIdentityError(RuntimeError):
    """The exact runtime build identity could not be determined —
    production IPC must not enter READY (frozen contract item 3)."""


# -- deterministic build identity (frozen contract item 3) -------------------


def compute_runtime_build_id(src_root: Optional[Path] = None) -> str:
    """A deterministic 256-bit identity of the actual running source.

    Hashes every ``*.py`` file under the source tree in sorted path
    order — package version is NOT a build identity. The value changes
    when any source byte changes, so two processes with different code
    can never handshake as the same build. Raises IPCBuildIdentityError
    when the tree cannot be read (ambiguous identity = no READY IPC)."""
    if src_root is None:
        src_root = Path(__file__).resolve().parent.parent
    try:
        files = sorted(p for p in src_root.rglob("*.py") if p.is_file())
        if not files:
            raise IPCBuildIdentityError(
                f"no source files found under {src_root}; build identity "
                "is ambiguous — production IPC must not enter READY"
            )
        digest = hashlib.sha256()
        for path in files:
            digest.update(str(path.relative_to(src_root)).encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        return digest.hexdigest()
    except IPCBuildIdentityError:
        raise
    except OSError as exc:
        raise IPCBuildIdentityError(
            f"could not hash the source tree for build identity: {exc!r}; production IPC must not enter READY"
        ) from exc


# -- handshake (frozen contract item 3) ---------------------------------------


@dataclass(frozen=True)
class AuthorityHello:
    """The owner's handshake: exact-build, exact-protocol, current-owner
    diagnostics. Every field is owner-supplied truth; the client verifies
    rather than trusting caller-supplied environment strings.

    Wire field names follow the frozen §10 shape exactly (F-65):
    ``supported_ipc_operations`` and ``state`` — not the Python attribute
    names, which stay descriptive internally."""

    protocol_version: int
    runtime_version: str  # diagnostic only (sys.version)
    runtime_build_id: str  # EXACT artifact identity (compute_runtime_build_id)
    authority_instance_id: str  # the Layer-3 owner identity
    authority_domain: str  # canonical absolute state_dir
    supported_operations: frozenset[str]
    lifecycle_state: str  # the AuthoritySession state at hello time

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "runtime_version": self.runtime_version,
            "runtime_build_id": self.runtime_build_id,
            "authority_instance_id": self.authority_instance_id,
            "authority_domain": self.authority_domain,
            # F-65: the frozen wire names, not the Python attribute names
            "supported_ipc_operations": sorted(self.supported_operations),
            "state": self.lifecycle_state,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "AuthorityHello":
        if not isinstance(raw, dict):
            raise IPCProtocolError("hello must be a JSON object")
        required = {
            "protocol_version",
            "runtime_build_id",
            "authority_instance_id",
            "authority_domain",
            "supported_ipc_operations",
            "state",
        }
        missing = required - set(raw.keys())
        if missing:
            raise IPCProtocolError(f"hello missing fields: {sorted(missing)}")
        # F-65: strict validation — unknown fields and wrong types reject.
        allowed = required | {"runtime_version"}
        unknown = set(raw.keys()) - allowed
        if unknown:
            raise IPCProtocolError(f"hello has unknown fields: {sorted(unknown)}")
        if not isinstance(raw["protocol_version"], int):
            raise IPCProtocolError("hello protocol_version must be an integer")
        for field in ("runtime_build_id", "authority_instance_id", "authority_domain", "state"):
            if not isinstance(raw[field], str) or not raw[field]:
                raise IPCProtocolError(f"hello {field} must be a non-empty string")
        ops = raw["supported_ipc_operations"]
        if not isinstance(ops, list) or not all(isinstance(o, str) for o in ops):
            raise IPCProtocolError("supported_ipc_operations must be a list of strings")
        return cls(
            protocol_version=raw["protocol_version"],
            runtime_version=str(raw.get("runtime_version", "")),
            runtime_build_id=raw["runtime_build_id"],
            authority_instance_id=raw["authority_instance_id"],
            authority_domain=raw["authority_domain"],
            supported_operations=frozenset(ops),
            lifecycle_state=raw["state"],  # F-65: the frozen wire name
        )


# -- strict post-schema request validation + normalization (item 5) ----------

# Each operation's schema: field name -> (type, required, validator).
# Deliberately NARROWER than local capability aliases — no url/q/etc.

IPC_SCHEMA: dict[str, dict[str, tuple[type, bool, Any]]] = {
    "health": {},
    "read": {
        "post_url": (str, True, None),
    },
    "read_profile": {
        "handle": (str, True, None),
        "tab": (str, False, IPC_READ_TABS),
        "limit": (int, False, None),
        "include_retweets": (bool, False, None),
    },
    "read_thread": {
        "post_url": (str, True, None),
        "limit": (int, False, None),
    },
    "read_search": {
        "query": (str, True, None),
        "tab": (str, False, IPC_SEARCH_TABS),
        "limit": (int, False, None),
    },
}

_DEFAULTS: dict[str, dict[str, Any]] = {
    "health": {},
    "read": {},
    "read_profile": {"tab": "posts", "limit": 20, "include_retweets": True},
    "read_thread": {"limit": 20},
    "read_search": {"tab": "top", "limit": 20},
}


def _check_string(name: str, value: str) -> None:
    if not value or len(value.encode("utf-8")) > IPC_MAX_STRING_BYTES:
        raise IPCSchemaError(f"{name} must be a non-empty string of at most {IPC_MAX_STRING_BYTES} UTF-8 bytes")


def validate_and_normalize_request(operation: str, payload: Any) -> dict[str, Any]:
    """Strict schema validation and normalization BEFORE canonicalization.

    Rejects: unknown operations, non-object payloads, unknown fields,
    wrong scalar types, non-finite numbers, invalid enums, and
    unreasonable string/limit sizes. Returns the NORMALIZED payload with
    frozen defaults applied (§ item 5: health {}, read {post_url},
    read_profile {handle, tab='posts', limit=20, include_retweets=True},
    read_thread {post_url, limit=20}, read_search {query, tab='top',
    limit=20})."""
    if operation not in IPC_SUPPORTED_OPERATIONS:
        raise IPCSchemaError(
            f"unknown IPC operation {operation!r}; supported: {sorted(IPC_SUPPORTED_OPERATIONS)}"
        )
    if not isinstance(payload, dict):
        raise IPCSchemaError("request payload must be a JSON object")
    schema = IPC_SCHEMA[operation]
    unknown = set(payload.keys()) - set(schema.keys())
    if unknown:
        raise IPCSchemaError(
            f"unknown fields for {operation}: {sorted(unknown)} (the IPC "
            "schema is deliberately narrower than local aliases)"
        )
    normalized = dict(_DEFAULTS[operation])
    for name, (expected_type, required, enum) in schema.items():
        if name not in payload:
            if required:
                raise IPCSchemaError(f"{operation}.{name} is required")
            continue
        value = payload[name]
        if expected_type is int:
            # bools are ints in Python — reject explicitly.
            if isinstance(value, bool) or not isinstance(value, int):
                raise IPCSchemaError(f"{operation}.{name} must be an integer")
            if not math.isfinite(value) or value < 1 or value > IPC_MAX_LIMIT:
                raise IPCSchemaError(f"{operation}.{name} must be between 1 and {IPC_MAX_LIMIT}")
        elif expected_type is bool:
            if not isinstance(value, bool):
                raise IPCSchemaError(f"{operation}.{name} must be a boolean")
        elif expected_type is str:
            if not isinstance(value, str):
                raise IPCSchemaError(f"{operation}.{name} must be a string")
            _check_string(f"{operation}.{name}", value)
            if enum is not None and value not in enum:
                raise IPCSchemaError(f"{operation}.{name} must be one of {sorted(enum)}")
        else:  # pragma: no cover — schema definition error
            raise IPCSchemaError(f"internal schema error for {name}")
        normalized[name] = value
    return normalized


# -- canonical request identity (§10.5 / item 5) ----------------------------


def canonical_request_identity(operation: str, normalized_payload: dict[str, Any]) -> str:
    """One deterministic post-schema request identity.

    Canonical serialization sorts object keys, preserves list order, and
    includes protocol_version + operation + the NORMALIZED payload. It
    excludes request_id, runtime_build_id, and authority_instance_id
    (§10.5): the same validated payload under a different key order
    produces the SAME identity; a different request_id never changes it.
    """
    canonical = json.dumps(
        {
            "protocol_version": IPC_PROTOCOL_VERSION,
            "operation": operation,
            "payload": normalized_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
