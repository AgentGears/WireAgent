"""Token bucket — per-action-type + global write circuit breaker (review Q2).

Per-action buckets alone under-protect against mixed-action loops (a runaway
agent alternating like/bookmark/follow while staying under each individual
bucket). So we have BOTH per-action limits AND a global combined circuit breaker.

Token buckets are risk-tier-aware: the limits are keyed on (action_type) but
the global breaker treats all writes uniformly. For Phase 4 public-content
actions, the global public cap is much tighter.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from webwire.safety.models import RiskTier

logger = logging.getLogger(__name__)

__all__ = ["TokenBucket", "BucketLimits", "DEFAULT_LIMITS"]


@dataclass(frozen=True)
class BucketLimits:
    """Rate limits for one bucket."""
    max_count: int
    window_seconds: float


# Default limits per review Q2. Conservative for a personal tool; configurable.
_DEFAULTS = {
    # Per-action-type limits.
    "bookmark": BucketLimits(max_count=10, window_seconds=300),   # 10 / 5min
    "like": BucketLimits(max_count=10, window_seconds=300),       # 10 / 5min
    "follow": BucketLimits(max_count=5, window_seconds=300),
    "repost": BucketLimits(max_count=3, window_seconds=300),
    "post": BucketLimits(max_count=3, window_seconds=3600),       # 3 / hour
    "reply": BucketLimits(max_count=5, window_seconds=3600),
    "quote": BucketLimits(max_count=3, window_seconds=3600),
    # Deletion: generous enough to serve as compensation for creation flows
    # (3 posts + 5 replies + 3 quotes / hour worst case), tight enough that
    # a runaway mass-deletion loop stays bounded; global breaker caps anyway.
    "delete_post": BucketLimits(max_count=10, window_seconds=3600),
    # Global circuit breaker — all writes combined.
    "_global": BucketLimits(max_count=20, window_seconds=300),    # 20 / 5min
}

DEFAULT_LIMITS = _DEFAULTS


@dataclass
class _WindowState:
    """Sliding-window state for one bucket."""
    timestamps: list[float] = field(default_factory=list)


class TokenBucket:
    """Per-action + global token bucket. Records a timestamp on each acquire();
    enforces max_count within window_seconds via sliding window."""

    def __init__(self, limits: Optional[dict[str, BucketLimits]] = None) -> None:
        self._limits = limits or dict(_DEFAULTS)
        self._states: dict[str, _WindowState] = {}

    def acquire(self, action_type: str, risk_tier: RiskTier) -> tuple[bool, str]:
        """Try to acquire a token for action_type. Returns (allowed, reason).
        Checks BOTH the per-action bucket and the global breaker."""
        now = time.time()
        # Global check first (circuit breaker).
        g_ok, g_reason = self._check_bucket("_global", now)
        if not g_ok:
            return False, f"global_circuit_breaker: {g_reason}"
        # Per-action check.
        a_ok, a_reason = self._check_bucket(action_type, now)
        if not a_ok:
            return False, f"token_bucket({action_type}): {a_reason}"
        # Both passed — record in both.
        self._states.setdefault("_global", _WindowState()).timestamps.append(now)
        self._states.setdefault(action_type, _WindowState()).timestamps.append(now)
        return True, "ok"

    def _check_bucket(self, key: str, now: float) -> tuple[bool, str]:
        limits = self._limits.get(key)
        if limits is None:
            return True, "no_limit"  # unknown action: allow (risk registry gates separately)
        state = self._states.setdefault(key, _WindowState())
        # Prune timestamps outside the window.
        cutoff = now - limits.window_seconds
        state.timestamps = [t for t in state.timestamps if t >= cutoff]
        if len(state.timestamps) >= limits.max_count:
            return False, f"exceeded {limits.max_count}/{limits.window_seconds:.0f}s"
        return True, "ok"

    def remaining(self, action_type: str) -> Optional[int]:
        """Remaining tokens for an action type (for diagnostics)."""
        limits = self._limits.get(action_type)
        if limits is None:
            return None
        state = self._states.get(action_type)
        if state is None:
            return limits.max_count
        now = time.time()
        cutoff = now - limits.window_seconds
        active = [t for t in state.timestamps if t >= cutoff]
        return max(0, limits.max_count - len(active))

    # -- hydration ----------------------------------------------------------

    def hydrate_records(self, records: Iterable[dict[str, Any]]) -> int:
        """Rebuild budget consumption from journal write records (each carrying
        ``action_type`` and ``_epoch``). Returns the number of events replayed.

        Semantics (P0 hydration spec, decision 2): every journaled write
        invocation that reached the policy stage is replayed, including ones a
        gate later denied — so hydrated budgets are marginally more
        conservative than live accounting, never less protective. A crash
        mid-execute journals nothing, losing at most one line's accounting;
        the global breaker rehydrates from the same lines either way.
        """
        replayed = 0
        for rec in records:
            action = rec.get("action_type")
            epoch = rec.get("_epoch")
            if not action or epoch is None:
                continue
            self._states.setdefault("_global", _WindowState()).timestamps.append(float(epoch))
            if action in self._limits:
                self._states.setdefault(action, _WindowState()).timestamps.append(float(epoch))
            replayed += 1
        if replayed:
            logger.info("TokenBucket hydrated %d budget events from journal", replayed)
        return replayed
