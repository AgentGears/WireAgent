"""M7 Layer 4 — bounded UTF-8 JSON frame encoding/decoding.

One strict, non-executable wire format (frozen §16/§49): length-prefixed
UTF-8 JSON with centralized size ceilings. No pickle, no marshal, no
object deserialization, no callable reconstruction. Malformed UTF-8,
malformed JSON, non-object envelopes, truncated frames, over-limit
lengths, trailing bytes, and oversized payloads all reject cleanly with
``IPCProtocolError`` — never with partial reads or unbounded buffers.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

from webwire.authority_ipc_protocol import (
    IPC_MAX_FRAME_BYTES,
    IPC_MAX_REQUEST_BYTES,
    IPC_MAX_RESPONSE_BYTES,
    IPCProtocolError,
)

__all__ = [
    "FrameHeader",
    "HEADER_SIZE",
    "encode_frame",
    "encode_json_frame",
    "parse_frame_header",
    "decode_json_frame",
]

# 8-byte big-endian length prefix + payload. The length is the payload
# byte count; the ceiling is the frame constant from the protocol module.
_HEADER = struct.Struct(">Q")
HEADER_SIZE = _HEADER.size


@dataclass(frozen=True)
class FrameHeader:
    """Decoded frame header: the announced payload length."""

    length: int


def encode_frame(payload: bytes) -> bytes:
    """Encode one length-prefixed frame. The payload must already be
    bounded UTF-8 JSON bytes; the encoder enforces the frame ceiling."""
    if len(payload) > IPC_MAX_FRAME_BYTES:
        raise IPCProtocolError(f"frame payload {len(payload)} exceeds the {IPC_MAX_FRAME_BYTES}-byte ceiling")
    return _HEADER.pack(len(payload)) + payload


def parse_frame_header(data: bytes) -> tuple[FrameHeader, int]:
    """Parse and bound-check a frame header from the start of ``data``.
    Returns the header and the number of bytes consumed."""
    if len(data) < HEADER_SIZE:
        raise IPCProtocolError(f"truncated frame header: {len(data)} bytes (expected {HEADER_SIZE})")
    (length,) = _HEADER.unpack(data[:HEADER_SIZE])
    if length > IPC_MAX_FRAME_BYTES:
        raise IPCProtocolError(
            f"announced frame length {length} exceeds the {IPC_MAX_FRAME_BYTES}-byte ceiling"
        )
    return FrameHeader(length=length), HEADER_SIZE


def encode_json_frame(obj: Any, *, is_response: bool = False) -> bytes:
    """Serialize to strict UTF-8 JSON and frame it. Non-serializable or
    oversized objects reject before any bytes hit the wire."""
    ceiling = IPC_MAX_RESPONSE_BYTES if is_response else IPC_MAX_REQUEST_BYTES
    try:
        raw = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise IPCProtocolError(f"object is not strict-JSON encodable: {exc}") from exc
    if len(raw) > ceiling:
        raise IPCProtocolError(f"serialized payload {len(raw)} exceeds the {ceiling}-byte ceiling")
    return encode_frame(raw)


def decode_json_frame(header: FrameHeader, payload: bytes) -> Any:
    """Decode one frame's payload into a strict JSON value.

    Rejects: wrong payload length vs the header, malformed UTF-8,
    malformed JSON, trailing bytes after the JSON value, non-finite
    number constants, duplicate keys, and non-object top-level values."""
    if len(payload) != header.length:
        raise IPCProtocolError(
            f"payload length {len(payload)} does not match the announced frame length {header.length}"
        )
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise IPCProtocolError(f"frame payload is not valid UTF-8: {exc}") from exc
    try:
        value = json.loads(
            text,
            parse_constant=_reject_constant,
            object_pairs_hook=_reject_duplicate_keys,
        )
    except json.JSONDecodeError as exc:
        raise IPCProtocolError(f"frame payload is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise IPCProtocolError(
            "frame payload must be a JSON object (the wire protocol has no scalar/array envelopes)"
        )
    return value


def _reject_constant(name: str) -> Any:
    raise IPCProtocolError(f"non-finite JSON constant {name!r} is not permitted on the wire")


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise IPCProtocolError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)
