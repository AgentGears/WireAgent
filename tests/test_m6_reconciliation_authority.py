"""M6 Layer-4 operator-authority lifecycle regressions."""

from __future__ import annotations

from dataclasses import replace

import pytest

from webwire.safety import (
    ReconciliationAuthority,
    ReconciliationAuthorityError,
    ReconciliationRecord,
    ReconciliationVerdict,
    canonical_evidence_hash,
)


class _Clock:
    def __init__(self, value: float = 10.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _evidence() -> dict[str, object]:
    return {
        "basis": "operator-review",
        "observed_at": "2026-09-26T18:00:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }


def _record(
    *,
    evidence: dict[str, object] | None = None,
    reconciliation_id: str = "rec-1",
    timestamp: str = "2026-09-26T18:01:00+00:00",
) -> ReconciliationRecord:
    body = evidence or _evidence()
    return ReconciliationRecord(
        reconciliation_id=reconciliation_id,
        effect_id="fx-1",
        semantic_key="alice|like|post|123|",
        action_type="like",
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="alice",
        target_type="post",
        target_id="123",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        operator_id="operator-local",
        evidence_hash=canonical_evidence_hash(body),
        evidence=body,
        timestamp=timestamp,
    )


def _authority(clock: _Clock, *, ttl: float = 5.0) -> ReconciliationAuthority:
    evidence = _evidence()
    return ReconciliationAuthority(
        effect_id="fx-1",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=canonical_evidence_hash(evidence),
        operator_id="operator-local",
        ttl_seconds=ttl,
        monotonic_clock=clock,
        authority_id_factory=lambda: "auth-1",
    )


def test_uncommitted_authority_expires_on_monotonic_clock() -> None:
    clock = _Clock()
    authority = _authority(clock)
    clock.value = 15.0

    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.validate_start(
            effect_id="fx-1",
            verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence_hash=canonical_evidence_hash(_evidence()),
        )

    assert exc_info.value.reason == "authority_expired"
    assert authority.committed is False
    assert authority.consumed is False


def test_authority_binding_mismatch_denies_before_commit() -> None:
    clock = _Clock()
    authority = _authority(clock)

    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.validate_start(
            effect_id="different",
            verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence_hash=canonical_evidence_hash(_evidence()),
        )

    assert exc_info.value.reason == "effect_mismatch"


def test_committed_exact_fact_survives_later_ttl_expiry() -> None:
    clock = _Clock()
    authority = _authority(clock)
    record = _record()

    assert authority.validate_start(
        effect_id="fx-1",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=record.evidence_hash,
    ) is None
    authority.commit(record)

    clock.value = 10_000.0
    assert authority.validate_start(
        effect_id="fx-1",
        verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
        evidence_hash=record.evidence_hash,
    ) is record
    assert authority.committed_record is record
    assert authority.consumed is False


def test_committed_authority_rejects_changed_complete_fact() -> None:
    clock = _Clock()
    authority = _authority(clock)
    record = _record()
    authority.commit(record)

    changed_timestamp = replace(
        record,
        timestamp="2026-09-26T18:02:00+00:00",
    )
    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.commit(changed_timestamp)
    assert exc_info.value.reason == "committed_fact_mismatch"

    changed_id = replace(record, reconciliation_id="rec-2")
    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.commit(changed_id)
    assert exc_info.value.reason == "committed_fact_mismatch"


def test_consumed_authority_can_never_be_reused() -> None:
    clock = _Clock()
    authority = _authority(clock)
    record = _record()
    authority.commit(record)
    authority.consume(record)

    assert authority.consumed is True
    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.validate_start(
            effect_id="fx-1",
            verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence_hash=record.evidence_hash,
        )
    assert exc_info.value.reason == "authority_consumed"


def test_authority_clock_regression_fails_closed() -> None:
    clock = _Clock(20.0)
    authority = _authority(clock)
    clock.value = 19.0

    with pytest.raises(ReconciliationAuthorityError) as exc_info:
        authority.validate_start(
            effect_id="fx-1",
            verdict=ReconciliationVerdict.CONFIRMED_EFFECT,
            evidence_hash=canonical_evidence_hash(_evidence()),
        )

    assert exc_info.value.reason == "authority_clock_regressed"
