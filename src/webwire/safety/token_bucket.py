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

        del risk_tier  # tier is part of the caller contract; limits are action keyed today.
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
        state.timestamps = [timestamp for timestamp in state.timestamps if timestamp >= cutoff]
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

    def hydrate_records(self, records: Iterable[dict[str, Any]]) -> int:
        """Compatibility-only manual hydration; not used by live M5 Layer 7.

        The supported runtime never supplies journal rows here after Layer 7.
        This helper remains temporarily for explicit compatibility callers that
        already hold record data; such data is not M5 authority.
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
            logger.info("TokenBucket manually hydrated %d compatibility events", replayed)
        return replayed
