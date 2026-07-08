"""Metrics parser for X engagement counts.

Parses X's locale-formatted metric labels (e.g. '308416 Likes. Like',
'1.2K Replies. Reply', '5M reposts. Repost') into integers.

Order per the converged design (review Q3): abbreviation parser FIRST,
then full-digit parser, then None (unresolved) — never guess.
"""

from __future__ import annotations

import re

__all__ = ["parse_metric"]


# Map suffixes to multipliers. Case-insensitive.
_SUFFIX_MULT: dict[str, int] = {
    "k": 1_000,
    "m": 1_000_000,
    "b": 1_000_000_000,
}


def parse_metric(label: str | None) -> int | None:
    """Parse an X metric button label to an integer, or None if unresolved.

    X formats metrics as '<count> <Noun>. <Verb>' (e.g. '308416 Likes. Like').
    The count may be a full integer, comma-grouped ('308,416'), or abbreviated
    ('1.2K', '5M'). We extract the leading numeric token and resolve it.
    """
    if not label:
        return None
    # Capture the leading numeric token: digits, commas, periods, optional K/M/B.
    # The suffix (K/M/B) must be a STANDALONE token followed by a word boundary
    # — otherwise the 'B' in 'Bookmarks' gets read as a billions multiplier
    # (real bug caught live on jack/status/20: 21252 -> 21252000000000).
    m = re.match(r"\s*([\d.,]+)\s*([KkMmBb])\b", label)
    if not m:
        # No match with suffix — try a pure numeric prefix (full-digit form).
        m2 = re.match(r"\s*([\d.,]+)", label)
        if not m2:
            return None
        num_str = m2.group(1)
        suffix = None
    else:
        num_str = m.group(1)
        suffix = m.group(2)

    # Branch: abbreviation parser FIRST.
    if suffix:
        return _parse_abbreviated(num_str, suffix)

    # Full-digit parser: strip commas, validate it's an integer.
    cleaned = num_str.replace(",", "")
    # If there's a period but no suffix, it's ambiguous (e.g. '1.234' could be
    # a European thousand-grouped 1234 or a decimal 1.234). Be conservative:
    # accept only if it parses as a clean int after comma-strip.
    if "." in cleaned:
        # Could be European grouping or a decimal — ambiguous. Reject to avoid
        # guessing. (X rarely produces bare decimals without a K/M suffix.)
        return None
    try:
        return int(cleaned)
    except ValueError:
        return None


def _parse_abbreviated(num_str: str, suffix: str) -> int | None:
    """Resolve an abbreviated count like '1.2K' or '5M' to an integer."""
    mult = _SUFFIX_MULT.get(suffix.lower())
    if mult is None:
        return None
    try:
        # The numeric part is a decimal multiplier (e.g. 1.2 * 1000 = 1200).
        base = float(num_str.replace(",", ""))
    except ValueError:
        return None
    value = int(round(base * mult))
    return value
