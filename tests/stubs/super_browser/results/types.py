"""Stub of super_browser.results.types — the ActionResult envelope family.

Shape-derived from the REAL SDK (parity-enforced locally by
tests/test_stub_parity.py): enum members and values exact; ActionError and
ActionResult field-compatible for everything Agent-WebWire touches.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Optional


class SuccessCategory(StrEnum):
    NAVIGATION = "navigation"
    MUTATION = "mutation"
    INSPECTION = "inspection"
    ARTIFACT = "artifact"
    UNCHANGED = "unchanged"


class FailureCategory(StrEnum):
    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NAVIGATION = "navigation"
    SECURITY = "security"
    BROWSER_CRASH = "browser_crash"
    VALIDATION = "validation"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"
    STALE_REF = "stale_ref"
    ELEMENT_OBSCURED = "element_obscured"
    FRAME_DETACHED = "frame_detached"
    AUTH_REQUIRED = "auth_required"
    RATE_LIMITED = "rate_limited"


class ErrorCategory(StrEnum):
    TIMEOUT = "timeout"
    SELECTOR_NOT_FOUND = "selector_not_found"
    NAVIGATION = "navigation"
    SECURITY = "security"
    BROWSER_CRASH = "browser_crash"
    VALIDATION = "validation"
    CONTEXT_OVERFLOW = "context_overflow"
    UNKNOWN = "unknown"


@dataclass
class ActionError:
    category: ErrorCategory
    message: str
    selector: Optional[str] = None
    recoverable: bool = True
    retry_hint: Optional[str] = None


@dataclass
class ActionResult:
    ok: bool = False
    data: Any = None
    error: Optional[ActionError] = None
    success_category: Optional[SuccessCategory] = None
    failure_category: Optional[FailureCategory] = None
    # Field-compatible extras present on the real envelope (attribute access
    # parity; methods are intentionally absent — offline tests don't call them).
    meta: Optional[dict[str, Any]] = None
    next_actions: Optional[list[str]] = None
    page_change_summary: Optional[str] = None
    result_category: Optional[str] = None


def action_result(*, ok: bool, data: Any = None,
                  error: Optional[ActionError] = None) -> ActionResult:
    return ActionResult(ok=ok, data=data, error=error)


__all__ = [
    "SuccessCategory",
    "FailureCategory",
    "ErrorCategory",
    "ActionError",
    "ActionResult",
    "action_result",
]
