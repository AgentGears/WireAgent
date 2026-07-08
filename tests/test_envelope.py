"""Tests for the envelope adapter — failure constructors + ErrorCategory mapping."""

from __future__ import annotations

from super_browser.results import ErrorCategory
from super_browser.results.types import FailureCategory, SuccessCategory

from webwire.envelope import (
    auth_required,
    hard_failure,
    kill_switched,
    ok_result,
    policy_blocked,
    soft_failure,
    unsupported_capability,
)


def test_ok_result_default_inspection() -> None:
    r = ok_result(data={"handle": "x"})
    assert r.ok is True
    assert r.success_category == SuccessCategory.INSPECTION
    assert r.data == {"handle": "x"}


def test_soft_failure_recoverable_and_category_mapping() -> None:
    r = soft_failure("stale", failure_category=FailureCategory.STALE_REF)
    assert r.ok is False
    assert r.error.recoverable is True
    # STALE_REF is exclusive -> bucketed to SELECTOR_NOT_FOUND
    assert r.error.category == ErrorCategory.SELECTOR_NOT_FOUND
    assert r.failure_category == FailureCategory.STALE_REF


def test_soft_failure_timeout_identity_mapped() -> None:
    r = soft_failure("slow", failure_category=FailureCategory.TIMEOUT)
    assert r.error.category == ErrorCategory.TIMEOUT


def test_hard_failure_non_recoverable() -> None:
    r = hard_failure("broken", failure_category=FailureCategory.BROWSER_CRASH)
    assert r.ok is False
    assert r.error.recoverable is False
    assert r.error.category == ErrorCategory.BROWSER_CRASH


def test_auth_required() -> None:
    r = auth_required()
    assert r.ok is False
    assert r.failure_category == FailureCategory.AUTH_REQUIRED
    # AUTH_REQUIRED exclusive -> SECURITY
    assert r.error.category == ErrorCategory.SECURITY


def test_policy_blocked() -> None:
    r = policy_blocked()
    assert r.ok is False
    assert r.failure_category == FailureCategory.SECURITY


def test_kill_switched() -> None:
    r = kill_switched()
    assert r.ok is False
    assert r.failure_category == FailureCategory.SECURITY


def test_unsupported_capability() -> None:
    r = unsupported_capability("like")
    assert r.ok is False
    assert r.failure_category == FailureCategory.VALIDATION
    assert r.error.category == ErrorCategory.VALIDATION
    assert "like" in r.error.message
