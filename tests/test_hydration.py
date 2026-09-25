"""Layer-7 regressions: invocation journal is audit-only.

The pre-M5/P0 runtime rebuilt dedupe and token-bucket state from
``journal.ndjson``. Layer 7 intentionally retires that data path. These tests
pin the new authority split while preserving the live process-local controls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from webwire.config import WebWireConfig
from webwire.journal import Journal, JournalRecord, read_recent_write_records
from webwire.safety import (
    DedupeStore,
    EffectLedger,
    EffectLedgerRecord,
    EffectState,
    RecoveryGuard,
    RiskTier,
    TokenBucket,
)


def _audit_write(
    trace_id: str,
    *,
    action_type: str = "post",
    dedupe_key: str = "@actor|post|none|none|same",
) -> JournalRecord:
    return JournalRecord(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        trace_id=trace_id,
        capability="post_text",
        policy_decision="allowed",
        result_ok=True,
        capability_tier="write",
        action_type=action_type,
        risk_tier="public_content_irreversible",
        dedupe_key=dedupe_key,
    )


def _unknown_record(*, semantic_key: str) -> EffectLedgerRecord:
    return EffectLedgerRecord(
        effect_id="effect-unknown-1",
        semantic_key=semantic_key,
        state=EffectState.EFFECT_UNKNOWN,
        action_type="post",
        intent_hash="intent-hash",
        policy_binding="policy-binding",
        actor_id="@actor",
        target_type="none",
        target_id="none",
        details={"reason": "uncertain external outcome"},
    )


def test_journal_reader_is_retired_as_safety_input(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    journal = Journal(cfg)
    journal.append(_audit_write("audit-1"))

    assert cfg.journal_path().exists()
    assert read_recent_write_records(cfg.journal_path(), 0.0) == []


def test_dedupe_does_not_hydrate_from_journal_after_layer7(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    key = "@actor|post|none|none|same"
    Journal(cfg).append(_audit_write("audit-2", dedupe_key=key))

    store = DedupeStore(ttl_seconds=3600)
    assert store.hydrate_from_journal(cfg.journal_path()) == 0
    assert store.size() == 0
    assert store.check(key) is True


def test_token_bucket_does_not_replay_journal_budget_after_layer7(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    journal = Journal(cfg)
    for index in range(10):
        journal.append(_audit_write(f"audit-budget-{index}"))

    bucket = TokenBucket()
    records = read_recent_write_records(cfg.journal_path(), 0.0)
    assert bucket.hydrate_records(records) == 0
    assert bucket.remaining("post") == 3


def test_process_local_dedupe_still_blocks_live_duplicate() -> None:
    key = "@actor|post|none|none|same"
    store = DedupeStore(ttl_seconds=3600)

    assert store.check(key) is True
    store.record(key)
    assert store.check(key) is False


def test_process_local_token_bucket_still_enforces_limits() -> None:
    bucket = TokenBucket()
    tier = RiskTier.PUBLIC_CONTENT_IRREVERSIBLE

    for _ in range(3):
        allowed, _ = bucket.acquire("post", tier)
        assert allowed is True
    allowed, reason = bucket.acquire("post", tier)
    assert allowed is False
    assert "token_bucket(post)" in reason


def test_effect_ledger_unknown_blocks_independently_of_missing_journal(
    tmp_path: Path,
) -> None:
    semantic_key = "@actor|post|none|none|same"
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    ledger.append_durable(_unknown_record(semantic_key=semantic_key))

    guard = RecoveryGuard(ledger)
    status = guard.hydrate()

    assert status.available is True
    assert status.unresolved_semantic_keys == (semantic_key,)
    block = guard.require_clear(semantic_key, refresh=False)
    assert block is not None
    assert block.effect_ids == ("effect-unknown-1",)
    assert not (tmp_path / "journal.ndjson").exists()


def test_forged_journal_fact_cannot_create_recovery_block(tmp_path: Path) -> None:
    semantic_key = "@actor|post|none|none|same"
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    Journal(cfg).append(_audit_write("forged-audit", dedupe_key=semantic_key))

    guard = RecoveryGuard(EffectLedger(path=tmp_path / "effects.ndjson"))
    status = guard.hydrate()

    assert status.available is True
    assert status.unresolved_semantic_keys == ()
    assert guard.require_clear(semantic_key, refresh=False) is None


def test_corrupt_journal_cannot_change_recovery_projection(tmp_path: Path) -> None:
    semantic_key = "@actor|post|none|none|same"
    (tmp_path / "journal.ndjson").write_text("{definitely-not-json\n", encoding="utf-8")
    ledger = EffectLedger(path=tmp_path / "effects.ndjson")
    ledger.append_durable(_unknown_record(semantic_key=semantic_key))

    guard = RecoveryGuard(ledger)
    status = guard.hydrate()

    assert status.available is True
    assert status.unresolved_semantic_keys == (semantic_key,)
