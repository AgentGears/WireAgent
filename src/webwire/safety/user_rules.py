"""M8 user rule layer — the standing-decision store (frozen spec:
docs/M8_DESIGN.md, sections 4-5; layer 1 of the M8 build order).

Scope of THIS module: rules, selectors, deterministic matching, the tier
ceiling, and persistence. The kernel three-way gate (layer 2), the compiler
(layer 3), and the card surface (layer 4) are absent by design and compose
this module later.

Two safety properties are load-bearing here and test-locked:

- **The ceiling is a match-time downgrade.** An ALLOW rule matching an
  action above the risk-tier ceiling yields ASK. No store content can
  bypass it; creation-time checks are a convenience, not the enforcement.
- **Fail-open is "everything asks."** A missing, corrupt, or unreadable
  store yields zero active rules — exactly the pre-M8 behavior. A rule
  failure can never widen what rules permit.

No model runs anywhere in this module. Matching is set logic over the
structured fields the kernel already holds.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Optional

from webwire.safety.models import RiskTier

logger = logging.getLogger(__name__)

__all__ = [
    "RuleDecision",
    "RuleSelector",
    "UserRule",
    "RuleMatch",
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
    target_ids: Optional[frozenset[str]] = None
    actors: Optional[frozenset[str]] = None

    def __post_init__(self) -> None:
        # An unnamed scope is rejected the moment the selector exists, not
        # merely when a rule wraps it (spec section 4: "a rule must name its
        # scope" — enforced at the earliest boundary).
        self.validate()

    def validate(self) -> None:
        if not any((self.action_types, self.risk_tiers, self.target_ids, self.actors)):
            raise ValueError(
                "rule selector must name its scope: at least one of "
                "action_types/risk_tiers/target_ids/actors must be set"
            )
        for tier in self.risk_tiers or ():
            if not isinstance(tier, RiskTier):
                raise ValueError(f"risk_tiers entries must be RiskTier, got {tier!r}")

    def matches(
        self,
        *,
        action_type: str,
        risk_tier: RiskTier,
        target_id: str,
        actor: str,
    ) -> bool:
        if self.action_types is not None and action_type not in self.action_types:
            return False
        if self.risk_tiers is not None and risk_tier not in self.risk_tiers:
            return False
        if self.target_ids is not None and target_id not in self.target_ids:
            return False
        if self.actors is not None and actor not in self.actors:
            return False
        return True


@dataclass(frozen=True)
class UserRule:
    """One standing decision with mandatory TTL and provenance."""

    selector: RuleSelector
    decision: RuleDecision
    rule_id: str
    created_at: float
    expires_at: float
    provenance: str = "hand_written"  # hand_written | compiled
    source_text: str = ""

    def __post_init__(self) -> None:
        self.selector.validate()
        if not isinstance(self.decision, RuleDecision):
            raise ValueError(f"decision must be RuleDecision, got {self.decision!r}")
        if self.provenance not in ("hand_written", "compiled"):
            raise ValueError(f"provenance must be hand_written|compiled, got {self.provenance!r}")

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
    (temp file + os.replace), following the session-jar pattern. Rules are
    policy configuration, NOT effects; they never enter the effect ledger.

    Failure semantics: any read failure — missing file, malformed JSON,
    schema mismatch, invalid entries — yields zero active rules with a
    WARNING. Zero rules means every action asks, which is the pre-M8
    behavior. Invalid individual entries are skipped (with a warning), not
    fatal to the rest of the file: one bad rule must not silently disable
    the owner's NEVER rules.
    """

    def __init__(self, path: Path, clock: Any = time.time) -> None:
        self._path = Path(path)
        self._clock = clock
        self._cache: Optional[list[UserRule]] = None

    # -- persistence ---------------------------------------------------------

    def _ensure_parent(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("could not create rule store directory %s: %r", self._path.parent, exc)

    def save(self, rules: list[UserRule]) -> None:
        """Atomically replace the store contents."""
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "rules": [
                {
                    "rule_id": r.rule_id,
                    "decision": r.decision.value,
                    "created_at": r.created_at,
                    "expires_at": r.expires_at,
                    "provenance": r.provenance,
                    "source_text": r.source_text,
                    "selector": _selector_to_dict(r.selector),
                }
                for r in rules
            ],
        }
        self._ensure_parent()
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        try:
            tmp.write_text(json.dumps(payload, indent=1), encoding="utf-8")
            os.replace(tmp, self._path)
            self._cache = list(rules)
        except OSError as exc:
            logger.warning("rule store save failed: %r", exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self, *, force: bool = False) -> list[UserRule]:
        """All stored rules (valid ones; invalid entries skipped with a
        warning). Missing/corrupt file → empty list. Cached until save."""
        if self._cache is not None and not force:
            return list(self._cache)
        rules: list[UserRule] = []
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._cache = rules
            return list(rules)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("rule store unreadable (%s) — treating as empty; everything asks", exc)
            self._cache = rules
            return list(rules)
        if not isinstance(raw, dict) or raw.get("schema_version") != _SCHEMA_VERSION:
            logger.warning("rule store schema mismatch — treating as empty; everything asks")
            self._cache = rules
            return list(rules)
        for i, entry in enumerate(raw.get("rules", [])):
            try:
                rules.append(_rule_from_dict(entry))
            except (KeyError, TypeError, ValueError) as exc:
                logger.warning("rule store entry %d invalid, skipped: %r", i, exc)
        self._cache = rules
        return list(rules)

    # -- the matcher (deterministic; spec section 5) --------------------------

    def match(
        self,
        *,
        action_type: str,
        risk_tier: RiskTier,
        target_id: str,
        actor: str,
        now: Optional[float] = None,
    ) -> Optional[RuleMatch]:
        """The single decision input the gate consumes.

        Precedence over all matching, unexpired rules: never > ask > allow.
        The ceiling is applied as a match-time downgrade: an allow-match on
        an above-ceiling tier yields ask (ceiling_downgraded=True so the
        card can say so honestly).
        """
        t = now if now is not None else self._clock()
        best: Optional[tuple[int, UserRule]] = None
        for rule in self.load():
            if rule.is_expired(t):
                continue
            if rule.selector.matches(
                action_type=action_type, risk_tier=risk_tier,
                target_id=target_id, actor=actor,
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


def _rule_from_dict(entry: dict[str, Any]) -> UserRule:
    sel_raw = entry["selector"]
    risk_raw = sel_raw.get("risk_tiers")
    return UserRule(
        rule_id=str(entry["rule_id"]),
        decision=RuleDecision(entry["decision"]),
        created_at=float(entry["created_at"]),
        expires_at=float(entry["expires_at"]),
        provenance=str(entry.get("provenance", "hand_written")),
        source_text=str(entry.get("source_text", "")),
        selector=RuleSelector(
            action_types=_opt(sel_raw.get("action_types"), frozenset),
            risk_tiers=_opt(risk_raw, lambda vs: frozenset(RiskTier(v) for v in vs)),
            target_ids=_opt(sel_raw.get("target_ids"), frozenset),
            actors=_opt(sel_raw.get("actors"), frozenset),
        ),
    )

def _opt(value: Any, wrap: Any) -> Any:
    """Wrap a JSON value, or None if absent — selector fields are
    None-means-unspecified, never empty-means-unspecified."""
    return wrap(value) if value is not None else None


def _selector_to_dict(sel: RuleSelector) -> dict[str, Any]:
    risk = sorted(t.value for t in sel.risk_tiers) if sel.risk_tiers is not None else None
    return {
        "action_types": sorted(sel.action_types) if sel.action_types is not None else None,
        "risk_tiers": risk,
        "target_ids": sorted(sel.target_ids) if sel.target_ids is not None else None,
        "actors": sorted(sel.actors) if sel.actors is not None else None,
    }
