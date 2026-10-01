"""Safety subsystem — kill switch, write kernel, and effect controls.

The browser-dependent modules (kill_switch, scoped_authority, write_kernel
— all import the browser result types) are LAZY (PEP 562, F-44): importing
a browser-free submodule such as ``webwire.safety.m8_rule_lifecycle`` does
not transitively import them. Their names resolve on first attribute access
exactly as before.
"""

from typing import Any

from webwire.safety.confirmation_state import ConfirmationState
from webwire.safety.dedupe import DedupeStore
from webwire.safety.effect_ledger import (
    EffectLedger,
    EffectLedgerCorruptError,
    EffectLedgerError,
    EffectLedgerRecord,
    EffectState,
    RecoveryProjection,
)
from webwire.safety.effect_policy import (
    DEFAULT_EFFECT_POLICIES,
    DurabilityPolicy,
    EffectPolicy,
    EffectPolicyRegistry,
    EffectVerb,
    PreparationVerb,
    ReplaySemantics,
    derive_durability,
)
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
from webwire.safety.reconciliation_authority import (
    DEFAULT_RECONCILIATION_AUTHORITY_TTL_S,
    ReconciliationAuthority,
    ReconciliationAuthorityError,
)
from webwire.safety.reconciliation_ledger import (
    ReconciliationLedger,
    ReconciliationLedgerAmbiguousError,
    ReconciliationLedgerCorruptError,
    ReconciliationLedgerError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)
from webwire.safety.recovery_projector import (
    CompositeRecoveryProjection,
    ReconciliationPublicationFence,
    RecoveryDisposition,
    RecoveryProjector,
    RecoveryProjectorCorruptError,
    RecoveryProjectorError,
)
from webwire.safety.risk_registry import DEFAULT_REGISTRY, RiskRegistry
from webwire.safety.token_bucket import DEFAULT_LIMITS, BucketLimits, TokenBucket

_LAZY_IMPORTS: dict[str, tuple[str, str]] = {
    "ReconciliationCoordinator": ("webwire.safety.reconciliation_coordinator", "ReconciliationCoordinator"),
    "ReconciliationCoordinatorError": (
        "webwire.safety.reconciliation_coordinator",
        "ReconciliationCoordinatorError",
    ),
    "ReconciliationDenied": ("webwire.safety.reconciliation_coordinator", "ReconciliationDenied"),
    "ReconciliationPersistenceError": (
        "webwire.safety.reconciliation_coordinator",
        "ReconciliationPersistenceError",
    ),
    "ReconciliationPublicationError": (
        "webwire.safety.reconciliation_coordinator",
        "ReconciliationPublicationError",
    ),
    "ReconciliationResolution": ("webwire.safety.reconciliation_coordinator", "ReconciliationResolution"),
    "ReconciliationTarget": ("webwire.safety.reconciliation_coordinator", "ReconciliationTarget"),
    "ReconciliationOperatorError": ("webwire.safety.reconciliation_operator", "ReconciliationOperatorError"),
    "ReconciliationOperatorSession": (
        "webwire.safety.reconciliation_operator",
        "ReconciliationOperatorSession",
    ),
    "ReconciliationProposal": ("webwire.safety.reconciliation_operator", "ReconciliationProposal"),
    "RecoveryBlock": ("webwire.safety.recovery_guard", "RecoveryBlock"),
    "RecoveryEffect": ("webwire.safety.recovery_guard", "RecoveryEffect"),
    "RecoveryGuard": ("webwire.safety.recovery_guard", "RecoveryGuard"),
    "RecoveryGuardUnavailable": ("webwire.safety.recovery_guard", "RecoveryGuardUnavailable"),
    "RecoveryStatus": ("webwire.safety.recovery_guard", "RecoveryStatus"),
    "KillSwitch": ("webwire.safety.kill_switch", "KillSwitch"),
    "PreviewResult": ("webwire.safety.write_kernel", "PreviewResult"),
    "WriteCapability": ("webwire.safety.write_kernel", "WriteCapability"),
    "WriteKernel": ("webwire.safety.write_kernel", "WriteKernel"),
    "AuthorizedEffect": ("webwire.safety.scoped_authority", "AuthorizedEffect"),
    "ClearBookmarkAuthority": (
        "webwire.safety.scoped_authority",
        "ClearBookmarkAuthority",
    ),
    "ClearLikeAuthority": ("webwire.safety.scoped_authority", "ClearLikeAuthority"),
    "DeletePostAuthority": ("webwire.safety.scoped_authority", "DeletePostAuthority"),
    "PostPreparationAuthority": (
        "webwire.safety.scoped_authority",
        "PostPreparationAuthority",
    ),
    "QuotePreparationAuthority": (
        "webwire.safety.scoped_authority",
        "QuotePreparationAuthority",
    ),
    "ReplyPreparationAuthority": (
        "webwire.safety.scoped_authority",
        "ReplyPreparationAuthority",
    ),
    "ScopedAuthorityBroker": (
        "webwire.safety.scoped_authority",
        "ScopedAuthorityBroker",
    ),
    "ScopedAuthorityDenied": (
        "webwire.safety.scoped_authority",
        "ScopedAuthorityDenied",
    ),
    "SetBookmarkAuthority": (
        "webwire.safety.scoped_authority",
        "SetBookmarkAuthority",
    ),
    "SetLikeAuthority": ("webwire.safety.scoped_authority", "SetLikeAuthority"),
    "SubmitContentAuthority": (
        "webwire.safety.scoped_authority",
        "SubmitContentAuthority",
    ),
}


def __getattr__(name: str) -> Any:
    if name in _LAZY_IMPORTS:
        import importlib

        module_name, attr = _LAZY_IMPORTS[name]
        return getattr(importlib.import_module(module_name), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_LAZY_IMPORTS.keys()))


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
    "ConfirmationState",
    "PolicyDecision",
    "PolicyVerdict",
    "Visibility",
    "Reversibility",
    "Amplification",
    "EffectPolicy",
    "EffectPolicyRegistry",
    "DEFAULT_EFFECT_POLICIES",
    "EffectVerb",
    "PreparationVerb",
    "ReplaySemantics",
    "DurabilityPolicy",
    "derive_durability",
    "EffectLedger",
    "EffectLedgerRecord",
    "EffectLedgerError",
    "EffectLedgerCorruptError",
    "EffectState",
    "RecoveryProjection",
    "ReconciliationLedger",
    "ReconciliationLedgerError",
    "ReconciliationLedgerCorruptError",
    "ReconciliationLedgerAmbiguousError",
    "ReconciliationRecord",
    "ReconciliationVerdict",
    "canonical_evidence_hash",
    "DEFAULT_RECONCILIATION_AUTHORITY_TTL_S",
    "ReconciliationAuthority",
    "ReconciliationAuthorityError",
    "ReconciliationCoordinator",
    "ReconciliationCoordinatorError",
    "ReconciliationDenied",
    "ReconciliationPersistenceError",
    "ReconciliationPublicationError",
    "ReconciliationResolution",
    "ReconciliationTarget",
    "ReconciliationOperatorError",
    "ReconciliationOperatorSession",
    "ReconciliationProposal",
    "CompositeRecoveryProjection",
    "ReconciliationPublicationFence",
    "RecoveryDisposition",
    "RecoveryProjector",
    "RecoveryProjectorError",
    "RecoveryProjectorCorruptError",
    "RecoveryGuard",
    "RecoveryGuardUnavailable",
    "RecoveryBlock",
    "RecoveryEffect",
    "RecoveryStatus",
    "AuthorizedEffect",
    "ScopedAuthorityBroker",
    "ScopedAuthorityDenied",
    "PostPreparationAuthority",
    "ReplyPreparationAuthority",
    "QuotePreparationAuthority",
    "SetBookmarkAuthority",
    "ClearBookmarkAuthority",
    "SetLikeAuthority",
    "ClearLikeAuthority",
    "DeletePostAuthority",
    "SubmitContentAuthority",
]
