"""M8 user rule layer — the rule lifecycle surface (list, TTL re-confirm).

Deliberately BROWSER-FREE (F-44): this module imports only the store,
registry, and compiler-side modules, so the rules-only CLI works on a base
installation with no browser dependency at all. The card surface
(webwire.m8_cards) — which needs the browser result types — is imported
lazily and only by the card command.

The re-confirm contract (F-34): compare-and-swap against the exact reviewed
snapshot. The listing hands out the immutable ``UserRule``; re-confirmation
replaces that snapshot — and only that snapshot — under the store's
mutation fence.
"""

from __future__ import annotations

import math
import time
from typing import Any, Optional

from webwire.safety.m8_compiler import describe_compiled_rule
from webwire.safety.risk_registry import DEFAULT_REGISTRY, RiskRegistry
from webwire.safety.user_rules import (
    DEFAULT_RULE_TTL_S,
    RuleStore,
    UserRule,
)

__all__ = [
    "RuleCard",
    "describe_rule",
    "list_rules",
    "reconfirm_rule",
]


def describe_rule(rule: UserRule, *, registry: RiskRegistry = DEFAULT_REGISTRY) -> str:
    """The canonical structural description of ANY stored rule — the same
    renderer the compiler's owner-confirmation uses, applied to the rule's
    full original TTL window. Total over legal rules (F-35): unknown or
    stale action names render an explicit unverifiable-ceiling note instead
    of raising."""
    return describe_compiled_rule(
        rule.decision,
        rule.selector,
        rule.expires_at - rule.created_at,
        registry=registry,
    )


class RuleCard:
    """One listed rule: the display fields AND the exact immutable snapshot
    (``rule``) that a later re-confirmation is bound to (F-34/F-36)."""

    def __init__(
        self,
        *,
        rule: UserRule,
        remaining_seconds: float,
        registry: RiskRegistry = DEFAULT_REGISTRY,
    ) -> None:
        self.rule = rule
        self.rule_id = rule.rule_id
        self.decision = rule.decision.value
        self.provenance = rule.provenance
        self.source_text = rule.source_text  # the owner's words (F-36)
        self.remaining_seconds = remaining_seconds
        self.expired = remaining_seconds <= 0
        self._registry = registry

    @property
    def description(self) -> str:
        return describe_rule(self.rule, registry=self._registry)

    def render_text(self) -> str:
        base = f"{self.rule_id}: {self.description}"
        if self.source_text:
            base += f'\n  owner\'s words: "{self.source_text}"'
        if self.expired:
            base += "\n  EXPIRED"
        else:
            base += f"\n  {self.remaining_seconds:.0f}s remaining"
        return base

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "decision": self.decision,
            "provenance": self.provenance,
            "source_text": self.source_text,
            "description": self.description,
            "remaining_seconds": self.remaining_seconds,
            "expired": self.expired,
        }


def list_rules(
    store: RuleStore,
    *,
    now: Optional[float] = None,
    registry: RiskRegistry = DEFAULT_REGISTRY,
) -> list[RuleCard]:
    """Every stored rule with its remaining lifetime, in store order.

    Reading uses the enforcement reader: a corrupt store lists as zero
    rules (the same fail-safe every match sees), never raises. Each card
    carries the immutable rule snapshot the owner is reviewing."""
    t = now if now is not None else time.time()
    return [
        RuleCard(
            rule=r,
            remaining_seconds=max(0.0, r.expires_at - t),
            registry=registry,
        )
        for r in store.load()
    ]


def reconfirm_rule(
    store: RuleStore,
    expected: UserRule,
    *,
    ttl_seconds: float = DEFAULT_RULE_TTL_S,
    now: Optional[float] = None,
) -> UserRule:
    """Re-confirm the EXACT rule the owner reviewed (F-34): compare-and-swap
    under the store's mutation fence.

    ``expected`` is the immutable snapshot handed out by listing (or read
    fresh by the CLI in the same breath it displays it). The replacement
    keeps the same rule_id — attribution identity — the same selector,
    decision, provenance, and the owner's original words, with a fresh TTL
    window starting now. If ANYTHING about the stored rule changed since
    that snapshot (decision, scope, timestamps, deletion, corruption), the
    store raises with zero mutation and the owner must list again."""
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or not math.isfinite(ttl_seconds)
        or ttl_seconds <= 0
        or ttl_seconds > DEFAULT_RULE_TTL_S
    ):
        raise ValueError(
            f"ttl_seconds must be a finite positive number no greater than {DEFAULT_RULE_TTL_S:.0f}"
        )
    t = now if now is not None else time.time()
    replacement = UserRule(
        rule_id=expected.rule_id,
        decision=expected.decision,
        created_at=t,
        expires_at=t + float(ttl_seconds),
        provenance=expected.provenance,
        source_text=expected.source_text,
        selector=expected.selector,
    )
    return store.replace_if_current(expected, replacement)
