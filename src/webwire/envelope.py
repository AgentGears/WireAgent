"""Envelope adapter — reuses Super-Browser's ActionResult, adds only the
registry-boundary failure constructors Agent-WebWire needs.

Per the Phase 0a design review (Point 1):
- Reuse ``ActionResult`` as the outer envelope everywhere. Do NOT build a
  second capability envelope.
- Agent-WebWire additions live only at the registry/capability boundary:
    soft failure  -> ok=False, recoverable=True, failure_category in
                     {TIMEOUT, STALE_REF, ELEMENT_OBSCURED, FRAME_DETACHED}
    auth failure  -> failure_category=AUTH_REQUIRED
    policy / kill -> failure_category=SECURITY
    unsupported / -> failure_category=VALIDATION, before touching the browser
      registry miss

The success path returns the SDK's ActionResult unchanged.
"""

from __future__ import annotations

from typing import Any, Optional

from super_browser.results import ActionError, ErrorCategory
from super_browser.results.types import (
    ActionResult,
    FailureCategory,
    SuccessCategory,
    action_result,
)

__all__ = [
    "ActionResult",
    "ActionError",
    "FailureCategory",
    "SuccessCategory",
    "ok_result",
    "soft_failure",
    "hard_failure",
    "auth_required",
    "policy_blocked",
    "kill_switched",
    "unsupported_capability",
]


# ---------------------------------------------------------------------------
# Success
# ---------------------------------------------------------------------------

def ok_result(
    data: Any = None,
    *,
    success_category: SuccessCategory = SuccessCategory.INSPECTION,
    method: Optional[str] = None,
) -> ActionResult:
    """Successful capability result.

    Default ``success_category`` is INSPECTION (read). Write capabilities —
    when they exist in Phase 3+ — will pass MUTATION / NAVIGATION explicitly.
    """
    r = action_result(ok=True, data=data)
    r.success_category = success_category
    return r


# ---------------------------------------------------------------------------
# Failure constructors — registry/capability boundary only
# ---------------------------------------------------------------------------

# FailureCategory is a strict superset of ErrorCategory. The 5 exclusive members
# have no 1:1 ErrorCategory parent, so bucket them by recoverability intent
# rather than dropping to UNKNOWN (which loses signal).
_FAILURE_TO_ERROR: dict[FailureCategory, ErrorCategory] = {
    # identity-mapped (shared members)
    FailureCategory.TIMEOUT: ErrorCategory.TIMEOUT,
    FailureCategory.SELECTOR_NOT_FOUND: ErrorCategory.SELECTOR_NOT_FOUND,
    FailureCategory.NAVIGATION: ErrorCategory.NAVIGATION,
    FailureCategory.SECURITY: ErrorCategory.SECURITY,
    FailureCategory.BROWSER_CRASH: ErrorCategory.BROWSER_CRASH,
    FailureCategory.VALIDATION: ErrorCategory.VALIDATION,
    FailureCategory.CONTEXT_OVERFLOW: ErrorCategory.CONTEXT_OVERFLOW,
    FailureCategory.UNKNOWN: ErrorCategory.UNKNOWN,
    # exclusive members — bucketed
    FailureCategory.STALE_REF: ErrorCategory.SELECTOR_NOT_FOUND,
    FailureCategory.ELEMENT_OBSCURED: ErrorCategory.SELECTOR_NOT_FOUND,
    FailureCategory.FRAME_DETACHED: ErrorCategory.SELECTOR_NOT_FOUND,
    FailureCategory.AUTH_REQUIRED: ErrorCategory.SECURITY,
    FailureCategory.RATE_LIMITED: ErrorCategory.SECURITY,
}


def _to_error_category(fc: FailureCategory) -> ErrorCategory:
    """Map a (possibly exclusive) FailureCategory to its closest ErrorCategory."""
    return _FAILURE_TO_ERROR.get(fc, ErrorCategory.UNKNOWN)


def soft_failure(
    message: str,
    *,
    failure_category: FailureCategory = FailureCategory.TIMEOUT,
    retry_hint: Optional[str] = None,
    selector: Optional[str] = None,
) -> ActionResult:
    """Recoverable failure — the caller may retry.

    ``failure_category`` should be a transient/recoverable member:
    TIMEOUT (default), STALE_REF, ELEMENT_OBSCURED, FRAME_DETACHED.
    """
    r = action_result(
        ok=False,
        error=ActionError(
            category=_to_error_category(failure_category),
            message=message,
            selector=selector,
            recoverable=True,
            retry_hint=retry_hint,
        ),
    )
    r.failure_category = failure_category
    return r


def hard_failure(
    message: str,
    *,
    failure_category: FailureCategory = FailureCategory.UNKNOWN,
    selector: Optional[str] = None,
) -> ActionResult:
    """Non-recoverable failure — retry will not help."""
    r = action_result(
        ok=False,
        error=ActionError(
            category=_to_error_category(failure_category),
            message=message,
            selector=selector,
            recoverable=False,
        ),
    )
    r.failure_category = failure_category
    return r


def auth_required(message: str = "Authentication required — login wall or expired session.") -> ActionResult:
    """Login wall / auth redirect detected."""
    r = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.SECURITY,
            message=message,
            recoverable=False,
            retry_hint="Re-acquire an authenticated session via Super-Browser attach.",
        ),
    )
    r.failure_category = FailureCategory.AUTH_REQUIRED
    return r


def policy_blocked(message: str = "Action blocked by read-only policy.") -> ActionResult:
    """Read-only broker policy denied the action."""
    r = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.SECURITY,
            message=message,
            recoverable=False,
            retry_hint="This action is outside the read-only surface. Write capabilities land in Phase 3+.",
        ),
    )
    r.failure_category = FailureCategory.SECURITY
    return r


def kill_switched(message: str = "Kill switch tripped — capability execution refused.") -> ActionResult:
    """Kill switch is tripped. Returned before/within any capability run."""
    r = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.SECURITY,
            message=message,
            recoverable=False,
            retry_hint="Remove the kill file (.webwire/kill) or reset the in-process flag to resume.",
        ),
    )
    r.failure_category = FailureCategory.SECURITY
    return r


def unsupported_capability(name: str) -> ActionResult:
    """Capability not registered or not implemented in this phase.

    Returned *before* touching the browser.
    """
    r = action_result(
        ok=False,
        error=ActionError(
            category=ErrorCategory.VALIDATION,
            message=f"Unsupported capability: {name!r}. No write primitives exist in Phase 0a.",
            recoverable=False,
        ),
    )
    r.failure_category = FailureCategory.VALIDATION
    return r
