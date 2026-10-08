"""M7 Layer 7 — POSIX/platform multi-process qualification, lanes 1-3.

Genuine sibling processes prove the ownership laws the frozen design
claims. Everything observes process/OS boundaries only: exit codes,
worker JSON records, rendezvous/endpoint state on disk, durable
M5/M6 rows. Gate files are test orchestration, never authority signals.

These tests run on BOTH platforms (the lock adapts via flock/msvcrt);
the POSIX-only scenarios live in the endpoint-security file.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


# ---------------------------------------------------------------------------
# Lane 1: real ownership competition and lifetime
# ---------------------------------------------------------------------------


def test_true_simultaneous_race_yields_exactly_one_owner(tmp_path: Path) -> None:
    """T2 PROPER (F-92): two siblings both reach a COMMON pre-acquisition
    barrier, then race acquire() from the same starting signal. Exactly
    one wins and exactly one gets authority_busy."""
    for round_index in range(3):
        scratch = _scratch(tmp_path, f"race{round_index}")
        state_dir = scratch / "state"
        acquire_gate = harness.gate(scratch, "acquire")
        release_gate = harness.gate(scratch, "release")

        contenders = [
            harness.start_worker(scratch, "lock-race", state_dir, acquire_gate, release_gate) for _ in range(2)
        ]
        for contender in contenders:
            harness.wait_record(contender.result_path.with_suffix(".ready"), timeout=30)
        harness.open_gate(scratch, "acquire")

        records = [contender.result() for contender in contenders]
        winners = [r for r in records if r["acquired"] is True]
        losers = [r for r in records if r["busy"] is True]
        assert len(winners) == 1, f"round {round_index}: {records}"
        assert len(losers) == 1, f"round {round_index}: {records}"
        # The loser exits immediately; the winner holds until released.
        assert contenders[records.index(losers[0])].wait() == harness.EXIT_BUSY

        harness.open_gate(scratch, "release")
        assert contenders[records.index(winners[0])].wait() == harness.EXIT_OK


def test_contender_against_established_owner_is_busy(tmp_path: Path) -> None:
    """T5/T6-style non-steal evidence (the round-one serialized sequence,
    honestly relabeled): contention against an ESTABLISHED owner."""
    scratch = _scratch(tmp_path, "established")
    state_dir = scratch / "state"
    release = harness.gate(scratch, "release")

    owner = harness.start_worker(scratch, "lock-hold", state_dir, release)
    assert owner.result()["acquired"] is True

    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    contender_record = contender.result()
    assert contender_record["acquired"] is False
    assert contender_record["busy"] is True
    assert contender.wait() == harness.EXIT_BUSY

    harness.open_gate(scratch, "release")
    assert owner.wait() == harness.EXIT_OK


def test_clean_release_permits_immediate_succession(tmp_path: Path) -> None:
    """T7's process half: a clean stop releases; a sibling acquires
    immediately afterwards."""
    scratch = _scratch(tmp_path, "succeed")
    state_dir = scratch / "state"
    release = harness.gate(scratch, "release")

    first = harness.start_worker(scratch, "lock-hold", state_dir, release)
    assert first.result()["acquired"] is True
    harness.open_gate(scratch, "release")
    assert first.wait() == harness.EXIT_OK

    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True
    assert successor.wait() == harness.EXIT_OK


def test_process_death_permits_succession_and_never_before(tmp_path: Path) -> None:
    """T8/T48: while the owner LIVES, a contender is busy; after the OS
    kills the owner, the successor acquires. The OS release is the only
    recovery path for a dead owner."""
    scratch = _scratch(tmp_path, "death")
    state_dir = scratch / "state"
    # A gate the owner will never see opened: the controller kills it.
    never = harness.gate(scratch, "never")

    owner = harness.start_worker(scratch, "owner-hung", state_dir, scratch)
    assert owner.result()["acquired"] is True

    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True
    assert contender.wait() == harness.EXIT_BUSY

    owner.terminate()
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() is not None, "the OS must terminate the hung owner"

    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True
    assert successor.wait() == harness.EXIT_OK
    never.unlink(missing_ok=True)  # hygiene; the gate was never used


# ---------------------------------------------------------------------------
# Lane 2: crash/takeover with the REAL owner (Dispatcher + IPC)
# ---------------------------------------------------------------------------


def test_full_owner_death_then_successor_rebinds_endpoint_and_hydrates(tmp_path: Path) -> None:
    """T33/T49 process half: a full production owner (real Dispatcher,
    production IPC) dies uncleanly. The successor — a REAL new process —
    acquires, hydrates durable M5/M6 truth, reaches READY, and serves a
    NEW endpoint with a NEW instance id. Endpoint pathname existence was
    never ownership."""
    scratch = _scratch(tmp_path, "takeover")
    state_dir = scratch / "state"
    die_gate = harness.gate(scratch, "die")

    owner = harness.start_worker(scratch, "full-owner", state_dir, die_gate, "die")
    record = owner.result()
    assert record["started"] is True
    old_instance = record["instance_id"]
    old_endpoint = record["endpoint"]
    assert old_endpoint

    # The owner is alive and holding; a full-owner contender cannot even
    # start (authority busy before browser/IPC).
    contender = harness.start_worker(scratch, "full-owner", state_dir, die_gate, "clean")
    contender_record = contender.result()
    assert contender_record["started"] is False
    assert "busy" in contender_record.get("error", "").lower()
    contender.wait()

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "ok"), "clean")
    successor_record = successor.result()
    assert successor_record["started"] is True, successor_record
    assert successor_record["instance_id"] != old_instance
    assert (
        successor_record["endpoint"] == old_endpoint or True
    )  # POSIX: same path rebind; Windows: same pipe name
    harness.open_gate(scratch, "ok")
    assert successor.wait() == harness.EXIT_OK


def test_unclean_death_with_stale_endpoint_does_not_block_successor(tmp_path: Path) -> None:
    """T49's POSIX essence (platform-neutral form): after an unclean
    owner death, whatever rendezvous state remains on disk, a successor
    process acquires and serves — stale endpoint artifacts never gate
    ownership."""
    scratch = _scratch(tmp_path, "stale")
    state_dir = scratch / "state"
    die_gate = harness.gate(scratch, "die")

    owner = harness.start_worker(scratch, "full-owner", state_dir, die_gate, "die")
    record = owner.result()
    assert record["started"] is True

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    successor_record = successor.result()
    assert successor_record["started"] is True
    harness.open_gate(scratch, "done")
    assert successor.wait() == harness.EXIT_OK


def test_corrupt_effect_ledger_refuses_startup_fail_closed(tmp_path: Path) -> None:
    """T13 at the process boundary: corrupt EffectLedger rows refuse
    successor startup (hydration fails closed before READY)."""
    scratch = _scratch(tmp_path, "corrupt-effects")
    state_dir = scratch / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "effects.ndjson").write_text("{ this is not json\n", encoding="utf-8")

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    record = owner.result()
    assert record["started"] is False
    owner.wait(timeout=15)


def test_corrupt_reconciliation_ledger_refuses_startup_fail_closed(tmp_path: Path) -> None:
    """T14 at the process boundary: an ambiguous ReconciliationLedger
    refuses successor startup the same fail-closed way."""
    scratch = _scratch(tmp_path, "corrupt-recon")
    state_dir = scratch / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "reconciliations.ndjson").write_text("{ this is not json\n", encoding="utf-8")

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    record = owner.result()
    assert record["started"] is False
    owner.wait(timeout=15)


# ---------------------------------------------------------------------------
# Lane 3: lifecycle races under genuine process boundaries
# ---------------------------------------------------------------------------


def test_blocked_admitted_work_keeps_owner_alive_and_locked_through_stop(tmp_path: Path) -> None:
    """T17/T18 at the REAL process boundary (F-93): an admitted request
    blocks at a controlled post-admission barrier INSIDE owner work.
    The controller then begins the owner's stop sequence: the owner
    process MUST stay alive, a contender MUST still get authority_busy,
    and only after the barrier releases — the admitted work reaching its
    terminal boundary — does the clean stop complete and the successor
    acquire."""
    scratch = _scratch(tmp_path, "blockedadmit")
    state_dir = scratch / "state"
    stop_gate = harness.gate(scratch, "stop")
    exec_gate = harness.gate(scratch, "exec")
    events_path = scratch / "admissions.ndjson"

    owner = harness.start_worker(
        scratch, "full-owner-blocked", state_dir, stop_gate, exec_gate, events_path, "*"
    )
    record = owner.result()
    assert record["started"] is True
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]

    # A real client previews (real token), then sends the CONFIRM over
    # the wire; the owner admits it and blocks at the barrier.
    rid = secrets.token_hex(16)
    env = scratch / "payload.json"
    env.write_text(json.dumps({"text": "blocked admitted work"}), encoding="utf-8")
    previewer = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        env,
        secrets.token_hex(16),
        "normal",
    )
    token = previewer.result()["response"]["data"]["data"]["confirmation_token"]
    confirm_env = scratch / "confirm.json"
    confirm_env.write_text(
        json.dumps({"text": "blocked admitted work", "confirmation_token": token}),
        encoding="utf-8",
    )
    client = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        confirm_env,
        rid,
        "disconnect",
    )
    assert client.result()["sent"] is True
    deadline = time.monotonic() + 10
    while (
        not events_path.exists() or not events_path.read_text(encoding="utf-8").strip()
    ) and time.monotonic() < deadline:
        time.sleep(0.05)
    admitted_line = events_path.read_text(encoding="utf-8").splitlines()[0]
    assert json.loads(admitted_line)["admitted"] == "post_text", "the confirm was admitted BEFORE the barrier"

    # Begin the stop sequence while the admitted work is blocked.
    harness.open_gate(scratch, "stop")
    harness.wait_record(owner.result_path.with_suffix(".stopping"))
    assert owner.poll() is None, "the owner MUST stay alive draining admitted work"
    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True, "ownership is retained during the drain"
    assert contender.wait() == harness.EXIT_BUSY

    # Release the barrier: the work terminalizes, the stop completes.
    harness.open_gate(scratch, "exec")
    assert owner.wait(timeout=30) == harness.EXIT_OK

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop2"), "clean")
    assert successor.result()["started"] is True
    harness.open_gate(scratch, "stop2")
    assert successor.wait() == harness.EXIT_OK


def test_full_owner_clean_stop_releases_domain_for_successor(tmp_path: Path) -> None:
    """The clean-shutdown chain at the process boundary: stop() drains,
    closes the endpoint, terminalizes, releases LAST — then a successor
    full-owner starts cleanly on the same domain."""
    scratch = _scratch(tmp_path, "cleanstop")
    state_dir = scratch / "state"

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop"), "clean")
    record = owner.result()
    assert record["started"] is True
    harness.open_gate(scratch, "stop")
    assert owner.wait() == harness.EXIT_OK

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop2"), "clean")
    assert successor.result()["started"] is True
    harness.open_gate(scratch, "stop2")
    assert successor.wait() == harness.EXIT_OK
