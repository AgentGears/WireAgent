"""M8 user rule layer — the card surface (frozen spec: docs/M8_DESIGN.md,
section 8; layer 4 of the M8 build order).

A minimal card-style interface over the existing phase-1/phase-2 flow: it
renders the preview as a card (summary, warnings, matched rule), takes
approve/deny, and holds the confirmation token internally. Plus the rule
lifecycle surface the build order names: list, TTL re-confirm.

Scope discipline (frozen spec): the CLI scope is exactly what cards need —
this is not a workflow framework. Two deliberate non-features:

- **No new authority.** Every decision still comes from the WriteKernel
  through the caller's invoke callable (normally the Dispatcher); the card
  only renders, remembers the token, and replays phase 2 on approval.
- **No rule removal.** The build order names exactly two lifecycle
  operations — list and TTL re-confirm. Revocation stays what it is today:
  editing the store (or deleting it), observed by the very next match.

The confirmation token is treated as the authority it is: the card holds it
internally, never renders it, and never appears in ``repr``.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Optional

from super_browser.results import ActionError, ErrorCategory, action_result
from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult
from webwire.safety.m8_compiler import describe_compiled_rule
from webwire.safety.user_rules import (
    DEFAULT_RULE_TTL_S,
    RuleStore,
    RuleStoreError,
    UserRule,
)

__all__ = [
    "ApprovalCard",
    "CardFlow",
    "RuleCard",
    "describe_rule",
    "list_rules",
    "reconfirm_rule",
]

_Invoke = Callable[[str, dict[str, Any]], Awaitable[ActionResult]]


class ApprovalCard:
    """One pending human decision, rendered from a phase-1 response.

    The confirmation token lives inside the card and never leaves it except
    as the phase-2 replay the owner approved. A card is single-use: approve
    or deny consumes it, and a second decision raises."""

    def __init__(
        self,
        *,
        capability_name: str,
        payload: dict[str, Any],
        summary: str,
        token: str,
        target_url: str = "",
        current_state: str = "",
        warnings: tuple[str, ...] = (),
        matched_rule: Optional[dict[str, Any]] = None,
        intent_hash: str = "",
        expires_at: float = 0.0,
    ) -> None:
        self.capability_name = capability_name
        self.summary = summary
        self.target_url = target_url
        self.current_state = current_state
        self.warnings = warnings
        self.matched_rule = matched_rule
        self.intent_hash = intent_hash
        self.expires_at = expires_at
        self._payload = dict(payload)
        self._token = token
        self._spent: Optional[str] = None

    def __repr__(self) -> str:
        # The token is authority, not display data.
        return (
            f"ApprovalCard(capability_name={self.capability_name!r}, "
            f"summary={self.summary!r}, matched_rule={self.matched_rule!r}, "
            f"spent={self._spent})"
        )

    def to_dict(self) -> dict[str, Any]:
        """The renderable card: everything the owner decides on, EXCEPT the
        confirmation token (held internally by design)."""
        return {
            "capability_name": self.capability_name,
            "summary": self.summary,
            "target_url": self.target_url,
            "current_state": self.current_state,
            "warnings": list(self.warnings),
            "matched_rule": dict(self.matched_rule) if self.matched_rule else None,
            "intent_hash": self.intent_hash,
            "expires_at": self.expires_at,
        }

    def render_text(self) -> str:
        lines = [f"[{self.capability_name}] {self.summary}"]
        if self.matched_rule:
            note = ""
            if self.matched_rule.get("ceiling_downgraded"):
                note = " (above the allow ceiling — still asks)"
            lines.append(
                f"matched rule: {self.matched_rule.get('matched_rule_id')}{note}"
            )
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        lines.append(f"expires at {self.expires_at:.0f}")
        return "\n".join(lines)

    def _consume(self, action: str) -> None:
        if self._spent is not None:
            raise RuntimeError(
                f"approval card for {self.capability_name!r} was already "
                f"{self._spent}: a card carries exactly one decision"
            )
        self._spent = action

    async def approve(self, invoke: _Invoke) -> ActionResult:
        """The owner approved: replay the exact original payload with the
        held token as phase 2."""
        self._consume("approved")
        payload = dict(self._payload)
        payload["confirmation_token"] = self._token
        return await invoke(self.capability_name, payload)

    def deny(self) -> ActionResult:
        """The owner declined: no invocation is made, the token simply
        expires unconsumed, and the card is spent."""
        self._consume("denied")
        result = action_result(
            ok=False,
            error=ActionError(
                category=ErrorCategory.SECURITY,
                message=f"denied by owner: {self.capability_name}",
                recoverable=False,
            ),
        )
        result.failure_category = FailureCategory.SECURITY
        return result


class CardFlow:
    """Begin a card-mediated write through any invoke callable.

    ``begin`` returns the phase-1 result plus a card when human confirmation
    is pending; a rule-ALLOW (no confirmation carrier) or a NEVER denial
    returns the outcome with NO card — there is nothing for an owner to
    approve."""

    def __init__(self, invoke: _Invoke) -> None:
        self._invoke = invoke

    async def begin(
        self,
        capability_name: str,
        payload: dict[str, Any],
    ) -> tuple[ActionResult, Optional[ApprovalCard]]:
        result = await self._invoke(capability_name, dict(payload))
        data = result.data if isinstance(result.data, dict) else {}
        policy = data.get("policy", {}) if isinstance(data.get("policy"), dict) else {}
        phase1 = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        token = phase1.get("confirmation_token")
        if policy.get("verdict") != "confirmation_required" or not token:
            return result, None
        card = ApprovalCard(
            capability_name=capability_name,
            payload=payload,
            summary=str(phase1.get("preview", "")),
            token=str(token),
            target_url=str(phase1.get("target_url", "") or ""),
            current_state=str(phase1.get("current_state", "") or ""),
            warnings=tuple(str(w) for w in phase1.get("warnings", []) or []),
            matched_rule=(
                dict(phase1["rule_gate"])
                if isinstance(phase1.get("rule_gate"), dict)
                else None
            ),
            intent_hash=str(phase1.get("intent_hash", "") or ""),
            expires_at=float(phase1.get("expires_at", 0.0) or 0.0),
        )
        return result, card


# -- rule lifecycle (frozen build order: list, TTL re-confirm) ---------------


def describe_rule(rule: UserRule) -> str:
    """The canonical structural description of ANY stored rule — the same
    renderer the compiler's owner-confirmation uses, applied to the rule's
    full original TTL window."""
    return describe_compiled_rule(
        rule.decision, rule.selector, rule.expires_at - rule.created_at
    )


class RuleCard:
    """One listed rule with its live TTL state."""

    def __init__(
        self,
        *,
        rule_id: str,
        decision: str,
        provenance: str,
        description: str,
        remaining_seconds: float,
        expired: bool,
    ) -> None:
        self.rule_id = rule_id
        self.decision = decision
        self.provenance = provenance
        self.description = description
        self.remaining_seconds = remaining_seconds
        self.expired = expired

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "decision": self.decision,
            "provenance": self.provenance,
            "description": self.description,
            "remaining_seconds": self.remaining_seconds,
            "expired": self.expired,
        }


def list_rules(store: RuleStore, *, now: Optional[float] = None) -> list[RuleCard]:
    """Every stored rule with its remaining lifetime, in store order.

    Reading uses the enforcement reader: a corrupt store lists as zero
    rules (the same fail-safe every match sees), never raises."""
    t = now if now is not None else time.time()
    return [
        RuleCard(
            rule_id=r.rule_id,
            decision=r.decision.value,
            provenance=r.provenance,
            description=describe_rule(r),
            remaining_seconds=max(0.0, r.expires_at - t),
            expired=r.is_expired(t),
        )
        for r in store.load()
    ]


def reconfirm_rule(
    store: RuleStore,
    rule_id: str,
    *,
    ttl_seconds: float = DEFAULT_RULE_TTL_S,
    now: Optional[float] = None,
) -> UserRule:
    """Re-confirm one rule: same rule_id, same selector and decision, a
    fresh TTL window starting now.

    The owner's explicit call IS the confirmation. Identity (rule_id) is
    preserved so existing approver attribution stays meaningful. The
    replacement goes through the store's fenced update_strict: a corrupt
    store refuses with bytes unchanged, and every OTHER rule survives in
    place. Re-confirming an expired rule re-establishes it — that is what
    re-confirmation means; the rule was inert, the owner chose to revive it."""
    if (
        isinstance(ttl_seconds, bool)
        or not isinstance(ttl_seconds, (int, float))
        or ttl_seconds <= 0
        or ttl_seconds > DEFAULT_RULE_TTL_S
    ):
        raise ValueError(
            f"ttl_seconds must be a positive number no greater than "
            f"{DEFAULT_RULE_TTL_S:.0f}"
        )
    t = now if now is not None else time.time()
    # Source read through the enforcement reader: a corrupt store fails
    # closed here as "unknown rule" (no widening path reaches the update).
    source = next((r for r in store.load() if r.rule_id == rule_id), None)
    if source is None:
        raise RuleStoreError(
            f"rule_id {rule_id!r} not found — nothing to re-confirm"
        )
    reconfirmed = UserRule(
        rule_id=source.rule_id,
        decision=source.decision,
        created_at=t,
        expires_at=t + float(ttl_seconds),
        provenance=source.provenance,
        source_text=source.source_text,
        selector=source.selector,
    )
    store.update_strict(reconfirmed)
    return reconfirmed
