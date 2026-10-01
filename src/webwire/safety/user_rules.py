"""M8 user rule layer — the standing-decision store (frozen spec:
docs/M8_DESIGN.md, sections 4-5; layer 1 of the M8 build order).

Scope of THIS module: rules, selectors, deterministic matching, the tier
ceiling, and persistence. The kernel three-way gate (layer 2), the compiler
(layer 3), and the card surface (layer 4) are absent by design and compose
this module later.

Safety properties load-bearing here and test-locked (the store contract was
amended after the first-pass fallback review of PR #20, findings F-01..F-07):

- **The ceiling is a match-time downgrade.** An ALLOW rule matching an
  action above the risk-tier ceiling yields ASK. No store content can
  bypass it.
- **Any invalid persisted entry voids the entire read** to zero rules.
  Partially trusting a policy document with a broken entry could skip a
  restrictive NEVER while keeping a permissive ALLOW — a rule failure must
  never widen what rules permit (F-01).
- **Every match reads current persisted policy.** No enforcement caching:
  a revoked ALLOW or a newly added NEVER must be observed by the very next
  match, in every process (F-02).
- **TTLs are finite and forward.** Non-finite timestamps (NaN, ±inf) and
  expires_at <= created_at are rejected at construction and at parse;
  JSON's acceptance of NaN/Infinity literals does not reach the matcher
  (F-03).
- **Persistence failures raise** (`RuleStoreError`): a failed NEVER write
  must never be mistaken for an installed ban (F-04).
- **Persisted records parse strictly.** Mandatory fields missing or
  malformed → invalid entry → whole read voids. Provenance is never
  manufactured (F-05).
- **Rule ids are non-empty and unique within a store** (F-07).

No model runs anywhere in this module. Matching is set logic over the
structured fields the kernel already holds.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

from webwire.safety.models import RiskTier

logger = logging.getLogger(__name__)

__all__ = [
    "RuleDecision",
    "RuleSelector",
    "UserRule",
    "RuleMatch",
    "RuleStoreError",
    "ALLOW_CEILING_TIERS",
    "DEFAULT_RULE_TTL_S",
    "RuleStore",
]

DEFAULT_RULE_TTL_S = 7 * 24 * 3600.0
_SCHEMA_VERSION = 1


class RuleDecision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    NEVER = "never"


class RuleStoreError(Exception):
    """A rule-store persistence operation failed. Raised, never logged away:
    a caller that cannot distinguish a failed NEVER write from an installed
    one may tell the owner a ban exists when it does not (F-04)."""


# Standing ALLOW may auto-approve only these tiers (spec section 2). Above
# the ceiling an allow-match downgrades to ask — enforced HERE, at match
# time, so no store content can bypass it.
ALLOW_CEILING_TIERS = frozenset({
    RiskTier.PRIVATE_REVERSIBLE,
    RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
})

_PRECEDENCE = {RuleDecision.NEVER: 2, RuleDecision.ASK: 1, RuleDecision.ALLOW: 0}


@dataclass(frozen=True)
class RuleSelector:
    """Structured scope. Every specified dimension must match; unspecified
    dimensions match anything. At least one dimension must be set."""

    action_types: Optional[frozenset[str]] = None
    risk_tiers: Optional[frozenset[RiskTier]] = None
    target_types: Optional[frozenset[str]] = None
    target_ids: Optional[frozenset[str]] = None
    actors: Optional[frozenset[str]] = None

    def __post_init__(self) -> None:
        # An unnamed scope is rejected the moment the selector exists, not
        # merely when a rule wraps it (spec section 4).
        self.validate()

    def validate(self) -> None:
        if not any((self.action_types, self.risk_tiers, self.target_types,
                    self.target_ids, self.actors)):
            raise ValueError(
                "rule selector must name its scope: at least one of "
                "action_types/risk_tiers/target_types/target_ids/actors must be set"
            )
        # Annotations are not runtime contracts (PR #20 hardening): a plain
        # string or mutable set would make matching rely on Python membership
        # semantics instead of guaranteed set semantics. Every specified
        # dimension must be a non-empty frozenset of the exact element type.
        for name in ("action_types", "target_types", "target_ids", "actors"):
            values = getattr(self, name)
            if values is None:
                continue
            if type(values) is not frozenset:
                raise ValueError(f"{name} must be a frozenset, got {type(values).__name__}")
            if not values:
                raise ValueError(f"{name} must not be empty — use None for unspecified")
            for v in values:
                if not isinstance(v, str) or not v:
                    raise ValueError(f"{name} entries must be non-empty strings, got {v!r}")
        if self.risk_tiers is not None:
            if type(self.risk_tiers) is not frozenset:
                raise ValueError(
                    f"risk_tiers must be a frozenset, got {type(self.risk_tiers).__name__}"
                )
            if not self.risk_tiers:
                raise ValueError("risk_tiers must not be empty — use None for unspecified")
            for tier in self.risk_tiers:
                if not isinstance(tier, RiskTier):
                    raise ValueError(f"risk_tiers entries must be RiskTier, got {tier!r}")

    def matches(
        self,
        *,
        action_type: str,
        risk_tier: RiskTier,
        target_type: str,
        target_id: str,
        actor: str,
    ) -> bool:
        """The frozen M8_DESIGN.md section 4 signature, verbatim."""
        if self.action_types is not None and action_type not in self.action_types:
            return False
        if self.risk_tiers is not None and risk_tier not in self.risk_tiers:
            return False
        if self.target_types is not None and target_type not in self.target_types:
            return False
        if self.target_ids is not None and target_id not in self.target_ids:
            return False
        if self.actors is not None and actor not in self.actors:
            return False
        return True


@dataclass(frozen=True)
class UserRule:
    """One standing decision with mandatory TTL, provenance, and id."""

    selector: RuleSelector
    decision: RuleDecision
    rule_id: str
    created_at: float
    expires_at: float
    provenance: str = "hand_written"  # hand_written | compiled
    source_text: str = ""

    def __post_init__(self) -> None:
        self.selector.validate()
        if not isinstance(self.rule_id, str) or not self.rule_id.strip():
            raise ValueError("rule_id must be a non-empty string")
        if not isinstance(self.decision, RuleDecision):
            raise ValueError(f"decision must be RuleDecision, got {self.decision!r}")
        if self.provenance not in ("hand_written", "compiled"):
            raise ValueError(f"provenance must be hand_written|compiled, got {self.provenance!r}")
        if not isinstance(self.source_text, str):
            raise ValueError("source_text must be a string")
        # F-03 + hardening parity: NaN/±inf parse cleanly from JSON and never
        # satisfy >=; bools are ints in Python so isfinite(True) passes while
        # the persisted parser rejects them. Construction and parsing enforce
        # the same contract: real, finite numbers only.
        for _name, _v in (("created_at", self.created_at), ("expires_at", self.expires_at)):
            if isinstance(_v, bool) or not isinstance(_v, (int, float)):
                raise ValueError(f"{_name} must be a number, got {type(_v).__name__}")
            if not math.isfinite(_v):
                raise ValueError("created_at/expires_at must be finite timestamps")
        if self.expires_at <= self.created_at:
            raise ValueError("expires_at must be strictly greater than created_at")

    @classmethod
    def create(
        cls,
        *,
        selector: RuleSelector,
        decision: RuleDecision,
        rule_id: str,
        provenance: str = "hand_written",
        source_text: str = "",
        ttl_seconds: float = DEFAULT_RULE_TTL_S,
        now: Optional[float] = None,
    ) -> "UserRule":
        """The rule-creation boundary: applies the frozen default TTL."""
        t = now if now is not None else time.time()
        return cls(
            selector=selector, decision=decision, rule_id=rule_id,
            created_at=t, expires_at=t + ttl_seconds,
            provenance=provenance, source_text=source_text,
        )

    def is_expired(self, now: Optional[float] = None) -> bool:
        t = now if now is not None else time.time()
        return t >= self.expires_at


@dataclass(frozen=True)
class RuleMatch:
    """The gate consumes this: a decision plus the rule that supplied it."""

    decision: RuleDecision
    rule_id: str
    ceiling_downgraded: bool = False


class RuleStore:
    """Persistent, file-backed store of user rules.

    Persistence: a JSON document at the configured path, written atomically
    (temp file + os.replace). Rules are policy configuration, NOT effects;
    they never enter the effect ledger.

    Enforcement-read semantics (amended store contract, PR #20 review):
    - load() reads the file EVERY call — no cache. Revocation freshness is
      a store invariant, not caller discipline.
    - Missing file → zero rules (pre-M8 behavior; everything asks).
    - Malformed JSON / schema mismatch / ANY invalid entry / duplicate
      rule ids → zero rules for that entire read, with a WARNING. A policy
      document containing an invalid entry is not partially trusted.
    """

    def __init__(self, path: Path, clock: Callable[[], float] = time.time) -> None:
        self._path = Path(path)
        self._clock = clock

    # -- persistence ---------------------------------------------------------

    def save(self, rules: list[UserRule]) -> None:
        """Atomically replace the store contents.

        Raises RuleStoreError on any write failure (F-04). Refuses to save
        duplicate rule ids (F-07): ambiguous attribution is refused at the
        write boundary, not discovered at match time.
        """
        ids = [r.rule_id for r in rules]
        dupes = {i for i in ids if ids.count(i) > 1}
        if dupes:
            raise RuleStoreError(f"refusing to save duplicate rule ids: {sorted(dupes)}")
        payload = {"schema_version": _SCHEMA_VERSION, "rules": [_rule_to_dict(r) for r in rules]}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RuleStoreError(f"could not create rule store directory: {exc!r}") from exc
        # F-08: each save stages through a UNIQUE per-writer temporary file.
        # A shared "<store>.tmp" let two concurrent savers cross-contaminate:
        # writer A's os.replace could install writer B's bytes while A reports
        # success — believing a restrictive policy was installed while a
        # permissive one persists. Unique staging gives concurrent whole-store
        # saves normal last-writer-wins linearization: every successful
        # replace installs exactly that caller's payload.
        tmp = self._path.with_name(f"{self._path.name}.{uuid4().hex}.tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            _replace_with_windows_retry(tmp, self._path)
        except OSError as exc:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise RuleStoreError(f"rule store save failed: {exc!r}") from exc

    def load(self) -> list[UserRule]:
        """All stored rules — or ZERO on any invalid content (see class doc).
        Every call reads current persisted policy (F-02)."""
        empty: list[UserRule] = []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return empty
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("rule store unreadable (%s) — zero rules; everything asks", exc)
            return empty
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            logger.warning("rule store schema mismatch — zero rules; everything asks")
            return empty
        entries = raw.get("rules", [])
        if not isinstance(entries, list):
            logger.warning("rule store 'rules' not a list — zero rules; everything asks")
            return empty
        rules: list[UserRule] = []
        for i, entry in enumerate(entries):
            try:
                rules.append(_rule_from_dict(entry))
            except (KeyError, TypeError, ValueError) as exc:
                # F-01: one invalid entry voids the ENTIRE read. Skipping it
                # alone could drop a restrictive NEVER while a permissive
                # ALLOW stays live — widening what rules permit.
                logger.warning(
                    "rule store entry %d invalid (%s) — entire read voided; everything asks",
                    i, exc,
                )
                return empty
        ids = [r.rule_id for r in rules]
        if len(set(ids)) != len(ids):
            logger.warning("rule store has duplicate rule ids — entire read voided")
            return empty
        return rules

    # -- the matcher (deterministic; spec section 5) --------------------------

    def match(
        self,
        *,
        action_type: str,
        risk_tier: RiskTier,
        target_type: str,
        target_id: str,
        actor: str,
        now: Optional[float] = None,
    ) -> Optional[RuleMatch]:
        """The single decision input the gate consumes.

        Reads current persisted policy (fresh load; no cache). Precedence
        over all matching, unexpired rules: never > ask > allow. The ceiling
        is applied as a match-time downgrade: an allow-match on an
        above-ceiling tier yields ask (ceiling_downgraded=True so the card
        can say so honestly).
        """
        t = now if now is not None else self._clock()
        best: Optional[tuple[int, UserRule]] = None
        for rule in self.load():
            if rule.is_expired(t):
                continue
            if rule.selector.matches(
                action_type=action_type, risk_tier=risk_tier,
                target_type=target_type, target_id=target_id, actor=actor,
            ):
                rank = _PRECEDENCE[rule.decision]
                if best is None or rank > best[0]:
                    best = (rank, rule)
        if best is None:
            return None
        _, rule = best
        decision = rule.decision
        downgraded = False
        if decision is RuleDecision.ALLOW and risk_tier not in ALLOW_CEILING_TIERS:
            decision = RuleDecision.ASK
            downgraded = True
        return RuleMatch(decision=decision, rule_id=rule.rule_id, ceiling_downgraded=downgraded)


# ---------------------------------------------------------------------------
# Serialization (strict both directions)
# ---------------------------------------------------------------------------

def _rule_to_dict(r: UserRule) -> dict[str, Any]:
    return {
        "rule_id": r.rule_id,
        "decision": r.decision.value,
        "created_at": r.created_at,
        "expires_at": r.expires_at,
        "provenance": r.provenance,
        "source_text": r.source_text,
        "selector": _selector_to_dict(r.selector),
    }


def _num(value: Any, field: str) -> float:
    """Strict numeric parse: real numbers only — bools are ints in Python
    and JSON's NaN/Infinity literals parse as floats; both are rejected
    here so only finite timestamps reach UserRule."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field} must be a number, got {type(value).__name__}")
    return float(value)


def _str_list(value: Any, field: str) -> Optional[frozenset[str]]:
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise TypeError(f"{field} must be a list of strings when present")
    return frozenset(value)


def _rule_from_dict(entry: dict[str, Any]) -> UserRule:
    # F-05: strict parse. Every mandatory field must be present and well
    # typed; provenance is never manufactured and source_text is preserved
    # as the auditable, re-confirmable record of the owner's words.
    if not isinstance(entry, dict):
        raise TypeError("rule entry must be an object")
    for field_name in ("rule_id", "decision", "created_at", "expires_at",
                       "provenance", "source_text", "selector"):
        if field_name not in entry:
            raise KeyError(field_name)
    if not isinstance(entry["rule_id"], str):
        raise TypeError("rule_id must be a string")
    if not isinstance(entry["provenance"], str):
        raise TypeError("provenance must be a string")
    if not isinstance(entry["source_text"], str):
        raise TypeError("source_text must be a string")
    sel_raw = entry["selector"]
    if not isinstance(sel_raw, dict):
        raise TypeError("selector must be an object")
    risk_raw = sel_raw.get("risk_tiers")
    if risk_raw is not None:
        if not isinstance(risk_raw, list) or not all(isinstance(v, str) for v in risk_raw):
            raise TypeError("risk_tiers must be a list of strings when present")
        risk_tiers: Optional[frozenset[RiskTier]] = frozenset(RiskTier(v) for v in risk_raw)
    else:
        risk_tiers = None
    return UserRule(
        rule_id=entry["rule_id"],
        decision=RuleDecision(entry["decision"]),
        created_at=_num(entry["created_at"], "created_at"),
        expires_at=_num(entry["expires_at"], "expires_at"),
        provenance=entry["provenance"],
        source_text=entry["source_text"],
        selector=RuleSelector(
            action_types=_str_list(sel_raw.get("action_types"), "action_types"),
            risk_tiers=risk_tiers,
            target_types=_str_list(sel_raw.get("target_types"), "target_types"),
            target_ids=_str_list(sel_raw.get("target_ids"), "target_ids"),
            actors=_str_list(sel_raw.get("actors"), "actors"),
        ),
    )


def _opt_sorted(values: Optional[frozenset[str]]) -> Optional[list[str]]:
    return sorted(values) if values is not None else None


def _selector_to_dict(sel: RuleSelector) -> dict[str, Any]:
    risk = _opt_sorted(
        frozenset(t.value for t in sel.risk_tiers) if sel.risk_tiers is not None else None
    )
    return {
        "action_types": _opt_sorted(sel.action_types),
        "risk_tiers": risk,
        "target_types": _opt_sorted(sel.target_types),
        "target_ids": _opt_sorted(sel.target_ids),
        "actors": _opt_sorted(sel.actors),
    }


def _replace_with_windows_retry(src: Path, dst: Path, attempts: int = 8) -> None:
    """os.replace with a bounded retry for the Windows sharing violation.

    Two concurrent atomic replaces of the same destination are safe (each
    installs its own staged bytes or raises), but on Windows the loser can
    transiently get PermissionError (winerror 5/32) while the winner holds
    the target. A short bounded retry restores last-writer-wins
    linearization so concurrent savers both succeed. Any persistent error
    raises to the caller as usual.
    """
    for attempt in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.01 * (attempt + 1))
