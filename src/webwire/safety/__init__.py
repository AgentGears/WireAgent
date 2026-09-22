"""Safety subsystem — kill switch (Phase 0a) + write-safety kernel (Phase 0b)."""

from webwire.safety.dedupe import DedupeStore
from webwire.safety.kill_switch import KillSwitch
from webwire.safety.models import (
    Amplification,
    CompensationMeta,
    ConfirmationToken,
    PolicyDecision,
    PolicyVerdict,
    Reversibility,
    RiskMeta,
    RiskTier,
    Visibility,
    WriteIntent,
)
from webwire.safety.risk_registry import DEFAULT_REGISTRY, RiskRegistry
from webwire.safety.token_bucket import DEFAULT_LIMITS, BucketLimits, TokenBucket
from webwire.safety.write_kernel import PreviewResult, WriteCapability, WriteKernel

__all__ = [
    "KillSwitch",
    "WriteKernel",
    "WriteCapability",
    "PreviewResult",
    "WriteIntent",
    "RiskMeta",
    "RiskTier",
    "RiskRegistry",
    "DEFAULT_REGISTRY",
    "TokenBucket",
    "BucketLimits",
    "DEFAULT_LIMITS",
    "DedupeStore",
    "CompensationMeta",
    "ConfirmationToken",
    "PolicyDecision",
    "PolicyVerdict",
    "Visibility",
    "Reversibility",
    "Amplification",
]
