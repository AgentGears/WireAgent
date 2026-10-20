"""Layer 8 — L8-RACE01 diagnostic harness (INSTRUMENTS, not acceptance).

The frozen acceptance test (exactly one owner) is UNCHANGED. These
diagnostics exist to capture mechanism-level evidence for the Server-2025
both-busy failure: full exception chains with errno/winerror, worker PIDs,
resolved domain and lock-file identity, timestamps at every step, a
post-failure third contender, and a raw-primitive control arm (the same
simultaneous race against bare msvcrt/flock with NO AuthorityOwnerLock).

The tests PASS when the diagnostic records are captured; they deliberately
assert nothing about the winner count. Their records print into the CI log
via -s-free write + the workflow echo, and the JSON lands in the JUnit-adjacent
artifact directory.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def test_RACE01_diagnostic_authority_pair_and_third_probe(tmp_path: Path) -> None:
    """The production-side diagnostic: the common-gate pair through
    AuthorityOwnerLock with full capture, then a THIRD contender after the
    pair resolves — distinguishing persistent ownership from a transient
    acquisition failure."""
    scratch = _scratch(tmp_path, "race1")
    state_dir = scratch / "state"
    go = harness.gate(scratch, "go")
    release = harness.gate(scratch, "release")

    w1 = harness.start_worker(scratch, "race-diag", state_dir, go, release)
    w2 = harness.start_worker(scratch, "race-diag", state_dir, go, release)
    # both workers signal nothing before the gate; open it once both are
    # running (their spawn IS the barrier — the gate opens and both race).
    time.sleep(1.0)
    harness.open_gate(scratch, "go")

    r1 = harness.wait_record(w1.result_path, timeout=30)
    r2 = harness.wait_record(w2.result_path, timeout=30)
    diag = {"arm": "authority_pair", "r1": r1, "r2": r2}
    print("RACE01-DIAG authority pair:", json.dumps(diag, default=str))

    # Third contender AFTER the pair: persistent ownership or transient?
    probe = harness.start_worker(scratch, "lock-probe", state_dir)
    probe_rec = probe.result()
    diag["third_probe"] = probe_rec
    print("RACE01-DIAG third probe:", json.dumps(probe_rec, default=str))
    if probe_rec.get("acquired"):
        probe.wait()
    (scratch / "race-diag.json").write_text(json.dumps(diag, default=str, indent=2), encoding="utf-8")

    harness.open_gate(scratch, "release")
    w1.wait(timeout=30)
    w2.wait(timeout=30)

    # Instrument invariants only: records captured with the required fields.
    for rec in (r1, r2):
        assert "pid" in rec and "ts_go" in rec and "lock_path" in rec
        assert rec["acquired"] is True or ("exc_chain" in rec and rec["exc_chain"])


def test_RACE01_diagnostic_raw_primitive_pair(tmp_path: Path) -> None:
    """The control arm: the SAME simultaneous race against the RAW lock
    primitive, no AuthorityOwnerLock. If the raw pair also yields zero
    winners, the primitive (or the platform) is implicated; if it yields
    one, the production wrapper's layers are."""
    scratch = _scratch(tmp_path, "race2")
    lock_file = scratch / "raw.lock"
    lock_file.write_bytes(b"")
    go = harness.gate(scratch, "go")

    release = harness.gate(scratch, "release")
    w1 = harness.start_worker(scratch, "raw-lock-race", lock_file, go, release)
    w2 = harness.start_worker(scratch, "raw-lock-race", lock_file, go, release)
    time.sleep(1.0)
    harness.open_gate(scratch, "go")

    r1 = harness.wait_record(w1.result_path, timeout=30)
    r2 = harness.wait_record(w2.result_path, timeout=30)
    diag = {"arm": "raw_primitive_pair", "r1": r1, "r2": r2}
    winners = sum(1 for r in (r1, r2) if r.get("acquired"))
    diag["winners"] = winners
    print("RACE01-DIAG raw pair:", json.dumps(diag, default=str))
    (scratch / "race-raw.json").write_text(json.dumps(diag, default=str, indent=2), encoding="utf-8")

    harness.open_gate(scratch, "release")
    w1.wait(timeout=30)
    w2.wait(timeout=30)

    for rec in (r1, r2):
        assert rec["acquired"] is True or ("exc_chain" in rec and rec["exc_chain"])
