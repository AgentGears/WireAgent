"""Process-local per-action + global write circuit breaker.

Per-action buckets alone under-protect against mixed-action loops, so the live
process enforces both per-action limits and a global combined breaker.

M5 Layer 7 deliberately retires journal-backed restart hydration. Token-bucket
windows are process-local defense-in-depth; restarting the process resets them.
Durable uncertain-effect replay safety is a separate concern owned by
EffectLedger + RecoveryGuard. If cross-restart rate budgets are required in the
future, they need their own durable policy-state contract rather than the
best-effort invocation journal.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from webwire.safety.models import RiskTier

__all__ = ["TokenBucket", "BucketLimits", "DEFAULT_LIMITS"]


@dataclass(frozen=True)
class BucketLimits:
    """Rate limits for one bucket."""

    max_count: int
    window_seconds: float


_DEFAULTS = {
    "bookmark": BucketLimits(max_count=10, window_seconds=300),
    "like": BucketLimits(max_count=10, window_seconds=300),
    "follow": BucketLimits(max_count=5, window_seconds=300),
    "repost": BucketLimits(max_count=3, window_seconds=300),
    "post": BucketLimits(max_count=3, window_seconds=3600),
    "reply": BucketLimits(max_count=5, window_seconds=3600),
    "quote": BucketLimits(max_count=3, window_seconds=3600),
    "delete_post": BucketLimits(max_count=10, window_seconds=3600),
    "_global": BucketLimits(max_count=20, window_seconds=300),
}

DEFAULT_LIMITS = _DEFAULTS


@dataclass
class _WindowState:
    timestamps: list[float] = field(default_factory=list)


class TokenBucket:
    """Per-action + global in-memory rate limits for the current process."""

    def __init__(self, limits: Optional[dict[str, BucketLimits]] = None) -> None:
        self._limits = limits or dict(_DEFAULTS)
        self._states: dict[str, _WindowState] = {}

    def acquire(self, action_type: str, risk_tier: RiskTier) -> tuple[bool, str]:
        """Try to consume one live-process action/global token."""

        del risk_tier  # caller contract retains the tier; limits are action keyed today.
        now = time.time()
        global_ok, global_reason = self._check_bucket("_global", now)
        if not global_ok:
            return False, f"global_circuit_breaker: {global_reason}"
        action_ok, action_reason = self._check_bucket(action_type, now)
        if not action_ok:
            return False, f"token_bucket({action_type}): {action_reason}"
        self._states.setdefault("_global", _WindowState()).timestamps.append(now)
        self._states.setdefault(action_type, _WindowState()).timestamps.append(now)
        return True, "ok"

    def _check_bucket(self, key: str, now: float) -> tuple[bool, str]:
        limits = self._limits.get(key)
        if limits is None:
            return True, "no_limit"
        state = self._states.setdefault(key, _WindowState())
        cutoff = now - limits.window_seconds
        state.timestamps = [
            timestamp for timestamp in state.timestamps if timestamp >= cutoff
        ]
        if len(state.timestamps) >= limits.max_count:
            return False, f"exceeded {limits.max_count}/{limits.window_seconds:.0f}s"
        return True, "ok"

    def remaining(self, action_type: str) -> Optional[int]:
        """Remaining live-process tokens for an action type."""

        limits = self._limits.get(action_type)
        if limits is None:
            return None
        state = self._states.get(action_type)
        if state is None:
            return limits.max_count
        now = time.time()
        cutoff = now - limits.window_seconds
        active = [timestamp for timestamp in state.timestamps if timestamp >= cutoff]
        return max(0, limits.max_count - len(active))
