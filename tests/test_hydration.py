"""Hydration tests — P0 fix (2026-09-22): journal-sourced rebuild of BOTH
safety stores (dedupe memory + token-bucket budgets).

Spec decisions under test:
1. Dedupe key recorded on success OR uncertain submit (public_side_effect=True);
   gate-denied attempts journal no key.
2. Bucket budgets rehydrate from journaled write invocations (marginally
   conservative; never less protective).
3. Rotation (10 MB / 31 days, retain 6): hydration spans the rotation boundary.
4. Fail-open on missing/corrupt journal; torn tail logs a WARNING naming the
   file, and the rest still hydrates.
Plus the original regression: records written by the REAL Journal.append path
must hydrate (the 2026-09-22 review proved the old code hydrated 0 of them).
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest

from super_browser.results import ActionError, ErrorCategory, action_result

from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.journal import Journal, JournalRecord, read_recent_write_records
from webwire.safety import (
    DEFAULT_REGISTRY,
    DedupeStore,
    KillSwitch,
    RiskTier,
    TokenBucket,
    WriteIntent,
    WriteKernel,
)
from webwire.safety.write_kernel import PreviewResult


def _ts(minutes_ago: float = 0.0) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    ).isoformat(timespec="milliseconds")


def _write_record(
    trace_id: str,
    *,
    minutes_ago: float = 0.0,
    action_type: str = "bookmark",
    dedupe_key: Optional[str] = "?|bookmark|post|123|",
    capability: str = "bookmark_post",
) -> JournalRecord:
    return JournalRecord(
        timestamp=_ts(minutes_ago),
        trace_id=trace_id,
        capability=capability,
        policy_decision="allowed",
        result_ok=True,
        capability_tier="write",
        action_type=action_type,
        risk_tier="private_reversible",
        dedupe_key=dedupe_key,
    )


# ---------------------------------------------------------------------------
# 1 + 6. Dispatcher journals write facts; schema carries every field
# ---------------------------------------------------------------------------

class _KernelShapedResult:
    """Builds ActionResult data shaped like WriteKernel._finish output."""

    @staticmethod
    def build(*, dedupe_recorded: bool, action_type: str = "bookmark") -> Any:
        return ok_result(data={
            "policy": {"verdict": "allow", "risk_tier": "private_reversible"},
            "trace": {
                "action": "bookmark_post",
                "intent": {
                    "action_type": action_type,
                    "dedupe_key": "?|bookmark|post|123|",
                    "risk_tier": "private_reversible",
                },
                "dedupe_recorded": dedupe_recorded,
            },
        })


def test_dispatcher_journals_write_facts(tmp_path: Path) -> None:
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    cap = d._registry.get("bookmark_post")
    assert cap is not None

    facts = d._write_facts(cap, _KernelShapedResult.build(dedupe_recorded=True))
    assert facts == {
        "action_type": "bookmark",
        "risk_tier": "private_reversible",
        "dedupe_key": "?|bookmark|post|123|",
    }

    d._journal_write(
        trace_id="t1", capability="bookmark_post", input={"post_url": "https://x.com/a/status/1"},
        result=_KernelShapedResult.build(dedupe_recorded=True), policy_decision="allowed",
        actions=[], started_monotonic=time.monotonic(),
        capability_tier="write", **facts,
    )
    line = (tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip()
    rec = json.loads(line)
    assert rec["capability_tier"] == "write"
    assert rec["action_type"] == "bookmark"
    assert rec["risk_tier"] == "private_reversible"
    assert rec["dedupe_key"] == "?|bookmark|post|123|"


def test_gate_denied_write_journals_no_dedupe_key(tmp_path: Path) -> None:
    """A write the kernel did NOT record (dedupe_recorded absent/False) journals
    the action facts but no key — it must never hydrate as an executed write."""
    d = Dispatcher(WebWireConfig(state_dir=tmp_path, kill_env_var=None))
    cap = d._registry.get("bookmark_post")

    facts = d._write_facts(cap, _KernelShapedResult.build(dedupe_recorded=False))
    assert facts["action_type"] == "bookmark"
    assert facts["dedupe_key"] is None

    d._journal_write(
        trace_id="t2", capability="bookmark_post", input={},
        result=_KernelShapedResult.build(dedupe_recorded=False), policy_decision="allowed",
        actions=[], started_monotonic=time.monotonic(),
        capability_tier="write", **facts,
    )
    rec = json.loads((tmp_path / "journal.ndjson").read_text(encoding="utf-8").strip())
    assert rec["dedupe_key"] is None
    assert rec["action_type"] == "bookmark"

    # And a hydrating store ignores it.
    store = DedupeStore(ttl_seconds=3600)
    recs = read_recent_write_records(tmp_path / "journal.ndjson", time.time() - 3600)
    assert store.hydrate_records(recs) == 0


# ---------------------------------------------------------------------------
# 2. THE regression: real Journal.append output must hydrate
# ---------------------------------------------------------------------------

def test_hydrate_from_real_journal_pipeline(tmp_path: Path) -> None:
    """The 2026-09-22 review proved the old hydrate_from_journal read fields
    the journal never writes and hydrated 0 entries. Records produced by the
    REAL writer must now hydrate. (Appended in chronological order — the
    tail-scan's documented assumption, true of any real append-only journal.)"""
    j = Journal(WebWireConfig(state_dir=tmp_path))
    j.append(_write_record("r0", minutes_ago=120, dedupe_key="?|like|post|7|"))  # outside TTL
    j.append(_write_record("r1", minutes_ago=30, dedupe_key="?|like|post|9|", action_type="like"))
    j.append(_write_record("r2", minutes_ago=10, dedupe_key="?|like|post|8|", action_type="like"))

    store = DedupeStore(ttl_seconds=3600)
    n = store.hydrate_from_journal(tmp_path / "journal.ndjson")
    assert n == 2
    assert store.check("?|like|post|9|") is False   # duplicate — blocked
    assert store.check("?|like|post|8|") is False   # duplicate — blocked
    assert store.check("?|like|post|7|") is True    # outside window — allowed
    assert store.check("?|like|post|6|") is True    # never seen — allowed


# ---------------------------------------------------------------------------
# 3. Decision 1: degraded public submit records the key and blocks retry
# ---------------------------------------------------------------------------

class FakeUncertainPostCap:
    """Post capability whose execute returns a degraded result: submit clicked,
    outcome uncertain, public side effect possible."""

    name = "post_text"

    def compose(self, input: dict[str, Any], actor_identity: Optional[str]) -> WriteIntent:
        meta, comp = DEFAULT_REGISTRY.get("post")
        return WriteIntent(
            action_type="post", target_type="none", target_id="none",
            risk_meta=meta, compensation=comp,
            semantic_variant="abc123", actor_identity=actor_identity,
        )

    async def preview(self, intent: WriteIntent, broker: Any) -> PreviewResult:
        return PreviewResult(summary="Will post")

    async def execute(self, intent: WriteIntent, broker: Any) -> Any:
        r = action_result(
            ok=False,
            error=ActionError(
                ErrorCategory.UNKNOWN,
                "Submit clicked but result uncertain.",
                recoverable=False,
            ),
        )
        r.data = {
            "result": "submit_clicked_verification_pending",
            "public_side_effect": True,
        }
        return r

    async def verify(self, intent: WriteIntent, broker: Any) -> Any:
        return ok_result(data={"verified": False})


def test_degraded_public_submit_records_and_blocks_retry(tmp_path: Path) -> None:
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    kernel = WriteKernel(
        KillSwitch(cfg), DEFAULT_REGISTRY, TokenBucket(),
        DedupeStore(ttl_seconds=3600), Journal(cfg),
    )
    cap = FakeUncertainPostCap()
    dedupe = kernel._dedupe
    import asyncio

    # Phase 1: confirmation required.
    r1 = asyncio.run(kernel.execute(cap, object(), {"text": "hello"}))
    token = r1.data["data"]["confirmation_token"]

    # Phase 2: execute returns degraded (uncertain submit) — must still record.
    r2 = asyncio.run(kernel.execute(
        cap, object(), {"text": "hello", "confirmation_token": token}
    ))
    assert r2.ok is False
    assert r2.data["trace"]["dedupe_recorded"] is True
    assert dedupe.size() == 1

    # Retry within TTL: denied at the dedupe gate, before confirmation.
    r3 = asyncio.run(kernel.execute(cap, object(), {"text": "hello"}))
    assert r3.ok is False
    assert r3.data["policy"]["blocked_by"] == "dedupe"


# ---------------------------------------------------------------------------
# 4. Decision 2: bucket budgets rehydrate
# ---------------------------------------------------------------------------

def test_bucket_hydration_reduces_budgets(tmp_path: Path) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path))
    for i in range(3):
        j.append(_write_record(
            f"p{i}", action_type="post", dedupe_key=f"?|post|none|none|v{i}",
            capability="post_text",
        ))
    # A read-tier record must not count.
    j.append(JournalRecord(
        timestamp=_ts(0), trace_id="rd", capability="whoami",
        policy_decision="allowed", result_ok=True,
    ))

    bucket = TokenBucket()
    recs = read_recent_write_records(tmp_path / "journal.ndjson", time.time() - 3600)
    assert bucket.hydrate_records(recs) == 3
    assert bucket.remaining("post") == 0
    allowed, reason = bucket.acquire("post", RiskTier.PUBLIC_CONTENT_IRREVERSIBLE)
    assert allowed is False
    assert "token_bucket(post)" in reason


# ---------------------------------------------------------------------------
# 5. Decision 3: hydration spans the rotation boundary
# ---------------------------------------------------------------------------

def test_rotation_boundary_hydration(tmp_path: Path) -> None:
    active = tmp_path / "journal.ndjson"
    rotated = tmp_path / "journal-2026-08.ndjson"

    rotated_lines = [
        json.dumps({
            "timestamp": _ts(120), "trace_id": "old1", "capability": "bookmark_post",
            "policy_decision": "allowed", "capability_tier": "write",
            "action_type": "bookmark", "dedupe_key": "?|bookmark|post|1|",
        }),
        json.dumps({
            "timestamp": _ts(30), "trace_id": "old2", "capability": "bookmark_post",
            "policy_decision": "allowed", "capability_tier": "write",
            "action_type": "bookmark", "dedupe_key": "?|bookmark|post|2|",
        }),
    ]
    active_lines = [
        json.dumps({
            "timestamp": _ts(5), "trace_id": "new1", "capability": "bookmark_post",
            "policy_decision": "allowed", "capability_tier": "write",
            "action_type": "bookmark", "dedupe_key": "?|bookmark|post|3|",
        }),
    ]
    rotated.write_text("\n".join(rotated_lines) + "\n", encoding="utf-8")
    active.write_text("\n".join(active_lines) + "\n", encoding="utf-8")

    recs = read_recent_write_records(active, time.time() - 3600)
    keys = {r["dedupe_key"] for r in recs}
    # post|1 is 2h old (outside), post|2 spans the rotation (inside), post|3 active.
    assert keys == {"?|bookmark|post|2|", "?|bookmark|post|3|"}

    store = DedupeStore(ttl_seconds=3600)
    assert store.hydrate_records(recs) == 2


# ---------------------------------------------------------------------------
# 6. Decision 4: torn tail warns and the rest still hydrates
# ---------------------------------------------------------------------------

def test_torn_tail_warns_and_hydrates(tmp_path: Path, caplog) -> None:
    j = Journal(WebWireConfig(state_dir=tmp_path))
    j.append(_write_record("good", dedupe_key="?|bookmark|post|42|"))
    # Simulate a torn write: half a JSON line.
    with (tmp_path / "journal.ndjson").open("a", encoding="utf-8") as f:
        f.write('{"timestamp": "2026-09-22T00:00:00.000+00:00", "trace_i')

    with caplog.at_level("WARNING", logger="webwire.journal"):
        recs = read_recent_write_records(tmp_path / "journal.ndjson", time.time() - 3600)
    assert len(recs) == 1
    assert recs[0]["dedupe_key"] == "?|bookmark|post|42|"
    assert "not valid JSON" in caplog.text
    assert "journal.ndjson" in caplog.text

    store = DedupeStore(ttl_seconds=3600)
    assert store.hydrate_records(recs) == 1


def test_missing_journal_fails_open(tmp_path: Path) -> None:
    """No journal at all → empty stores, full budgets. Documented fail-open."""
    recs = read_recent_write_records(tmp_path / "journal.ndjson", time.time() - 3600)
    assert recs == []
    store = DedupeStore(ttl_seconds=3600)
    assert store.hydrate_records(recs) == 0
    bucket = TokenBucket()
    assert bucket.hydrate_records(recs) == 0
    assert bucket.acquire("bookmark", RiskTier.PRIVATE_REVERSIBLE)[0] is True


# ---------------------------------------------------------------------------
# 7. Rotation mechanics: size threshold, content preserved, never rewritten
# ---------------------------------------------------------------------------

def test_rotation_by_size(tmp_path: Path) -> None:
    # Measure one record's serialized size so the threshold is deterministic
    # (a full JournalRecord serializes well past 400 bytes).
    probe_dir = tmp_path / "probe"
    probe_dir.mkdir()
    probe = Journal(WebWireConfig(state_dir=probe_dir))
    probe.append(_write_record("probe"))
    record_bytes = len((probe_dir / "journal.ndjson").read_text(encoding="utf-8")) 
    threshold = 2 * record_bytes + 10  # rotate when a 3rd record would land

    j = Journal(
        WebWireConfig(state_dir=tmp_path),
        rotate_max_bytes=threshold, rotate_age_days=31.0, retain_rotated=6,
    )
    for i in range(6):
        j.append(_write_record(f"s{i}", dedupe_key=f"?|bookmark|post|{i}|"))

    rotated = j.rotated_paths()
    assert len(rotated) == 1
    total_lines = 0
    for p in [tmp_path / "journal.ndjson", *rotated]:
        total_lines += len([ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()])
    assert total_lines == 6, "rotation moves history; it never loses or rewrites it"
    # The rotated file holds the OLDER half; the active file got the rest.
    rotated_ids = {
        json.loads(ln)["trace_id"]
        for ln in rotated[0].read_text(encoding="utf-8").splitlines() if ln.strip()
    }
    assert rotated_ids == {"s0", "s1", "s2"}
