"""Text normalization for public content writes (Phase 4a).

The critical invariant (ChatGPT's Phase 4 invariant #1): preview text must equal
execution text. normalized_text = canonicalize(input_text). The confirmation
token binds normalized_text. Execute submits normalized_text only.

Normalization is DETERMINISTIC and STABLE: the same input always produces the
same normalized text and hash. No reformatting, smart punctuation, URL expansion,
or mutation after confirmation.

What normalization does (deliberately minimal):
- Strip trailing/leading whitespace.
- Normalize line endings to \\n.
- Collapse internal whitespace runs (spaces/tabs) to a single space WITHIN a line
  (but preserve paragraph breaks — double newlines).

What normalization does NOT do:
- No smart quotes, no autocorrect, no URL expansion, no emoji substitution.
- No truncation (if text exceeds X's limit, that's a policy rejection, not silent truncation).
- No HTML escaping (X's composer handles that).
"""

from __future__ import annotations

import hashlib
import re

__all__ = ["normalize_text", "text_hash", "validate_length"]


def normalize_text(text: str) -> str:
    """Canonicalize post text for token binding + execution.

    Deterministic: same input → same output, always.
    """
    if not text:
        return ""
    # Normalize line endings to \n.
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    # Strip leading/trailing whitespace.
    normalized = normalized.strip()
    # Collapse runs of spaces/tabs within each line to a single space,
    # but preserve paragraph breaks (blank lines).
    lines = normalized.split("\n")
    cleaned_lines = []
    for line in lines:
        # Collapse internal whitespace (spaces + tabs) to single space.
        cleaned = re.sub(r"[ \t]+", " ", line).strip()
        cleaned_lines.append(cleaned)
    normalized = "\n".join(cleaned_lines)
    # Remove trailing blank lines.
    normalized = normalized.rstrip("\n")
    return normalized


def text_hash(normalized: str) -> str:
    """Stable hash of normalized text for dedupe keys + token binding."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:32]


# X's character limit (standard posts). Reserved for policy validation.
_X_CHAR_LIMIT = 280


def validate_length(normalized: str) -> tuple[bool, int]:
    """Check if normalized text is within X's character limit.

    Returns (is_valid, char_count). Does NOT truncate — if over limit, the
    policy stage rejects, not silently shortens.
    """
    char_count = len(normalized)
    return (char_count <= _X_CHAR_LIMIT, char_count)
