"""M8 user rule layer — the card surface (frozen spec: docs/M8_DESIGN.md,
section 8; layer 4 of the M8 build order).

A minimal card-style interface over the existing phase-1/phase-2 flow: it
renders the preview as a card (summary, warnings, matched rule), takes
approve/deny, and holds the confirmation token internally.

The rule lifecycle surface (list, TTL re-confirm) lives in
``webwire.safety.m8_rule_lifecycle`` — browser-free by design so the
rules-only CLI works without the browser dependency — and is re-exported
here for callers that already hold the card surface.

The one principle carried over from Layer 3 (F-34..F-47): human-visible
state, confirmation authority, and the mutation performed afterward are all
bound to one immutable snapshot.

- **Token custody is an API property, not a rendering property.** The
  result returned to the caller is sanitized UNCONDITIONALLY, before any
  envelope branching (F-47): the confirmation token is removed from the
  payload and the policy echo regardless of shape, so a drifted or
  malformed envelope cannot leak it. The token exists only inside the
  card, which is bound to the invoke callable that created it —
  ``approve()`` takes no arguments and replays phase 2 through that exact
  route. ``deny()`` invokes nothing; the kernel token expires unconsumed
  (the confirmation subsystem exposes no per-token cancellation, and its
  bounded TTL bounds the denial).
- **The replayed payload is the phase-1 payload.** The request is frozen
  (deep-copied) BEFORE the first await (F-43) and deep-copied again at
  replay — mutation of the caller's structures during the phase-1 await
  window or after it cannot change what is confirmed.

Scope discipline (frozen spec): the CLI scope is exactly what cards need —
this is not a workflow framework. Rule removal is deliberately absent: the
build order names exactly list and TTL re-confirm; revocation remains
editing the store, observed by the very next match.
"""

from __future__ import annotations

import copy
from typing import Any, Awaitable, Callable, Optional

from super_browser.results import ActionError, ErrorCategory, action_result
from super_browser.results.types import FailureCategory

from webwire.envelope import ActionResult
from webwire.safety.m8_rule_lifecycle import (  # noqa: F401 — re-exported surface
    RuleCard,
    describe_rule,
    list_rules,
    reconfirm_rule,
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


def _scrub_token(result: ActionResult) -> ActionResult:
    """Return a copy of the result with the confirmation token removed from
    every location the kernel may put it — the payload
    (data.data.confirmation_token) and the policy echo
    (data.policy.confirmation_token, regardless of its runtime type).
    UNCONDITIONAL (F-47): called before any envelope inspection, so no
    drifted or malformed shape can return the raw token to the caller."""
    sanitized = copy.copy(result)
    if not isinstance(result.data, dict):
        return sanitized
    data = copy.deepcopy(result.data)
    payload = data.get("data")
    if isinstance(payload, dict):
        payload.pop("confirmation_token", None)
    policy = data.get("policy")
    if isinstance(policy, dict):
        policy.pop("confirmation_token", None)
    sanitized.data = data
    return sanitized


class ApprovalCard:
    """One pending human decision, rendered from a phase-1 response.

    Bound to the invoke callable and the exact payload that produced phase
    1. The confirmation token lives inside the card and never leaves it
    except as the phase-2 replay the owner approved. A card is single-use:
    approve or deny consumes it, and a second decision raises."""

    def __init__(
        self,
        *,
        invoke: _Invoke,
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
        self._invoke = invoke
        # The frozen request snapshot (F-43): captured by CardFlow BEFORE
        # the first await; the card never aliases caller-owned structures.
        self._payload = copy.deepcopy(payload)
        self._token = token
        self._spent: Optional[str] = None

    def __repr__(self) -> str:
        # The token is authority, not display data.
        return (
            f"ApprovalCard(capability_name={self.capability_name!r}, "
            f"summary={self.summary!r}, matched_rule={self.matched_rule!r}, "
            f"spent={self._spent!r})"
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
            lines.append(f"matched rule: {self.matched_rule.get('matched_rule_id')}{note}")
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

    async def approve(self) -> ActionResult:
        """The owner approved: replay the exact phase-1 payload with the
        held token, through the SAME invoke route that produced it (F-38:
        the card is bound at begin; there is no caller-selected transport)."""
        self._consume("approved")
        payload = copy.deepcopy(self._payload)
        payload["confirmation_token"] = self._token
        return await self._invoke(self.capability_name, payload)

    def deny(self) -> ActionResult:
        """The owner declined: no invocation is made, the kernel token
        expires unconsumed (the confirmation subsystem exposes no per-token
        cancellation; its bounded TTL is the denial's bound), and the card
        is spent."""
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

    ``begin`` returns a SANITIZED result (token removed unconditionally —
    F-40/F-47) plus a card bound to this flow's invoke when human
    confirmation is pending; a rule-ALLOW (no confirmation carrier) or a
    NEVER denial returns the outcome with NO card — there is nothing for an
    owner to approve. A confirmation-required response that has drifted so
    far it carries no usable token returns no card either (fail-closed: it
    cannot be approved through this surface)."""

    def __init__(self, invoke: _Invoke) -> None:
        self._invoke = invoke

    async def begin(
        self,
        capability_name: str,
        payload: dict[str, Any],
    ) -> tuple[ActionResult, Optional[ApprovalCard]]:
        # F-48: the card entry point never accepts pre-existing human
        # confirmation authority. A caller-supplied confirmation_token makes
        # the kernel treat this as PHASE 2 — the mutation would execute
        # before a card is ever rendered or a decision asked. Reject before
        # invoking anything (reject, not strip: silently stripping would
        # change caller intent).
        if "confirmation_token" in payload:
            raise ValueError(
                "CardFlow.begin() received a caller-supplied "
                "confirmation_token: confirmation authority may only be "
                "minted by phase 1 and held inside the card — remove the "
                "field and let the card mediate phase 2"
            )
        # F-43: freeze the request BEFORE the first await. Phase 1 and the
        # token describe this snapshot; the card replays this snapshot.
        request = copy.deepcopy(payload)
        result = await self._invoke(capability_name, copy.deepcopy(request))
        # Extract the token (if any) from the in-memory result, then scrub
        # unconditionally — the returned object never carries it.
        raw_data = result.data if isinstance(result.data, dict) else {}
        raw_payload = raw_data.get("data", {}) if isinstance(raw_data.get("data"), dict) else {}
        token = raw_payload.get("confirmation_token")
        sanitized = _scrub_token(result)

        data = sanitized.data if isinstance(sanitized.data, dict) else {}
        policy = data.get("policy", {}) if isinstance(data.get("policy"), dict) else {}
        phase1 = data.get("data", {}) if isinstance(data.get("data"), dict) else {}
        if policy.get("verdict") != "confirmation_required" or not token:
            return sanitized, None
        card = ApprovalCard(
            invoke=self._invoke,
            capability_name=capability_name,
            payload=request,
            summary=str(phase1.get("preview", "")),
            token=str(token),
            target_url=str(phase1.get("target_url", "") or ""),
            current_state=str(phase1.get("current_state", "") or ""),
            warnings=tuple(str(w) for w in phase1.get("warnings", []) or []),
            matched_rule=(dict(phase1["rule_gate"]) if isinstance(phase1.get("rule_gate"), dict) else None),
            intent_hash=str(phase1.get("intent_hash", "") or ""),
            expires_at=float(phase1.get("expires_at", 0.0) or 0.0),
        )
        return sanitized, card
