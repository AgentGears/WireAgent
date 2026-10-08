"""M7 Layer 7 round two — the durable-state crash matrix (T34/T36/T37),
the takeover-timing observer (T48), and the reconciliation-append crash
(T38). Real processes, real Dispatcher/WriteKernel/M5 executor, external
clients over the real transport, controlled death at each durable-state
boundary. The successor's durable M5/M6 truth governs in every case.
"""

from __future__ import annotations

import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402

_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)

MUTATION = "the round two controlled mutation"


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def _payload_file(scratch: Path, payload: dict) -> Path:
    path = scratch / f"payload-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _request(
    scratch: Path,
    endpoint: str,
    build: str,
    instance: str,
    operation: str,
    payload: dict,
    request_id: str | None = None,
    mode: str = "normal",
) -> harness.WorkerHandle:
    env = _payload_file(scratch, payload)
    return harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        operation,
        env,
        request_id or secrets.token_hex(16),
        mode,
    )


def _start_crash_owner(scratch: Path, state_dir: Path, point: str):
    owner = harness.start_worker(
        scratch, "full-owner-m5-crash-at", state_dir, harness.gate(scratch, "die"), point
    )
    record = owner.result()
    assert record["started"] is True, record
    return owner, record


def _drive_confirm_to_crash(scratch, record, owner, text: str) -> None:
    """External preview (real token) then external confirm-disconnect;
    wait until the owner reaches its crash point; release the death
    gate; wait for the unclean exit."""
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]
    preview = _request(scratch, endpoint, build, instance, "post_text", {"text": text})
    token = preview.result()["response"]["data"]["data"]["confirmation_token"]
    vanished = _request(
        scratch,
        endpoint,
        build,
        instance,
        "post_text",
        {"text": text, "confirmation_token": token},
        mode="disconnect",
    )
    assert vanished.result()["sent"] is True

    crash_marker = owner.result_path.with_suffix(".crash")
    deadline = time.monotonic() + 15
    while not crash_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert crash_marker.exists(), "the owner reached its crash point"
    harness.wait_record(crash_marker)

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN


def _start_successor(scratch: Path, state_dir: Path, tag: str):
    successor = harness.start_worker(
        scratch, "full-owner", state_dir, harness.gate(scratch, f"stop-{tag}"), "clean"
    )
    record = successor.result()
    assert record["started"] is True, record
    return successor, record


def _ledger_text(state_dir: Path) -> str:
    effects = state_dir / "effects.ndjson"
    return effects.read_text(encoding="utf-8") if effects.exists() else ""


# ---------------------------------------------------------------------------
# T34: crash BEFORE the durable reservation
# ---------------------------------------------------------------------------


def test_T34_pre_reserved_crash_fresh_confirmation_required(tmp_path: Path) -> None:
    """Crash during composer fill — the confirm was ADMITTED but no
    submit and NO durable row exists. The successor: starts cleanly
    (nothing unresolved), a fresh same-semantic write needs only a
    FRESH confirmation (no recovery denial — nothing to reconcile), and
    the OLD envelope/token can never reach execution (stale instance)."""
    scratch = _scratch(tmp_path, "t34")
    state_dir = scratch / "state"

    owner, record = _start_crash_owner(scratch, state_dir, "pre-reserved")
    _drive_confirm_to_crash(scratch, record, owner, MUTATION)

    assert "RESERVED" not in _ledger_text(state_dir), (
        "death before the commit gate leaves NO durable reservation"
    )

    successor, srecord = _start_successor(scratch, state_dir, "t34")
    try:
        # A fresh same-semantic write on the successor: NOT recovery-
        # blocked (nothing unresolved) — a fresh confirmation suffices.
        fresh = _request(
            scratch,
            srecord["endpoint"],
            srecord["build_id"],
            srecord["instance_id"],
            "post_text",
            {"text": MUTATION},
        )
        fresh_response = fresh.result()["response"]
        token_again = fresh_response.get("data", {}).get("data", {}).get("confirmation_token")
        assert token_again, f"a fresh confirmation is minted — nothing to reconcile: {fresh_response}"
        message = json.dumps(fresh_response).lower()
        assert (
            "reconciliation_required"
            not in message.replace("blocked_by", "").replace('"reconciliation_required"', "")
            or "blocked_by" not in message
        ), fresh_response

        # The OLD envelope (old instance) is dead on arrival.
        stale = _request(
            scratch,
            srecord["endpoint"],
            srecord["build_id"],
            record["instance_id"],
            "post_text",
            {"text": MUTATION},
        )
        stale_response = stale.result()["response"]
        assert stale_response["ok"] is False
        assert stale_response["error"]["code"] == "stale_authority_instance"
    finally:
        harness.open_gate(scratch, "stop-t34")
        successor.wait()


# ---------------------------------------------------------------------------
# T36: crash AFTER the external effect, BEFORE evidence
# ---------------------------------------------------------------------------


def test_T36_post_effect_crash_successor_recovery_governs(tmp_path: Path) -> None:
    """The click RETURNED success (the external effect plausibly exists)
    and death lands before ANY evidence capture. The durable ledger
    holds the unresolved RESERVED attempt; the successor hydrates it and
    the recovery gate REFUSES the same-semantic write — the uncertainty
    is resolved by M5/M6 truth, never by transport re-attempt."""
    scratch = _scratch(tmp_path, "t36")
    state_dir = scratch / "state"

    owner, record = _start_crash_owner(scratch, state_dir, "post-effect")
    _drive_confirm_to_crash(scratch, record, owner, MUTATION)

    crash = json.loads(owner.result_path.with_suffix(".crash").read_text(encoding="utf-8"))
    assert crash["at"] == "before_evidence_capture", crash
    assert "RESERVED" in _ledger_text(state_dir), "the attempt is durably unresolved"

    successor, srecord = _start_successor(scratch, state_dir, "t36")
    try:
        replay = _request(
            scratch,
            srecord["endpoint"],
            srecord["build_id"],
            srecord["instance_id"],
            "post_text",
            {"text": MUTATION},
        )
        replay_response = replay.result()["response"]
        assert replay_response["ok"] is False, replay_response
        blob = (replay_response["error"]["message"] + json.dumps(replay_response.get("safety", {}))).lower()
        assert any(word in blob for word in ("reconcil", "recovery", "unknown")), (
            f"the uncertain effect must be governed by durable recovery truth: {replay_response}"
        )
    finally:
        harness.open_gate(scratch, "stop-t36")
        successor.wait()


# ---------------------------------------------------------------------------
# T37: crash AFTER the durable terminal outcome
# ---------------------------------------------------------------------------


def test_T37_post_terminal_crash_settled_history_recovers(tmp_path: Path) -> None:
    """The full execution completes — verified evidence, EFFECT_CONFIRMED
    durable — and death lands after the terminal result. The successor
    hydrates the SETTLED history: the same-semantic write is NOT
    recovery-blocked (the key is resolved), and the old confirmation
    token died with the owner (a fresh one is required)."""
    scratch = _scratch(tmp_path, "t37")
    state_dir = scratch / "state"

    owner, record = _start_crash_owner(scratch, state_dir, "post-terminal")
    _drive_confirm_to_crash(scratch, record, owner, MUTATION)

    crash = json.loads(owner.result_path.with_suffix(".crash").read_text(encoding="utf-8"))
    assert crash["at"] == "after_terminal_result", crash
    assert "EFFECT_CONFIRMED" in _ledger_text(state_dir), "the terminal outcome is durable"

    successor, srecord = _start_successor(scratch, state_dir, "t37")
    try:
        settled = _request(
            scratch,
            srecord["endpoint"],
            srecord["build_id"],
            srecord["instance_id"],
            "post_text",
            {"text": MUTATION},
        )
        settled_response = settled.result()["response"]
        # Settled history: NO recovery denial — a fresh confirmation is
        # minted for a NEW attempt (the old token died with the owner).
        fresh_token = settled_response.get("data", {}).get("data", {}).get("confirmation_token")
        assert fresh_token, f"settled history recovers; the semantic key is clear: {settled_response}"
    finally:
        harness.open_gate(scratch, "stop-t37")
        successor.wait()


# ---------------------------------------------------------------------------
# T48: complete takeover timing — OS release, then hydrate BEFORE browser
# ---------------------------------------------------------------------------


def test_T48_successor_hydrates_before_browser_after_os_release(tmp_path: Path) -> None:
    """The full T48 chain at the process boundary: a HUNG full
    production owner holds the domain; the controller TERMINATES it (OS
    release is the only path); a successor full owner then acquires and
    its recorded start phases prove hydration happens BEFORE the browser
    stack is ever installed — corrupt-or-unreadable history can never
    become an empty replay-denial set behind a live browser."""
    scratch = _scratch(tmp_path, "t48")
    state_dir = scratch / "state"

    # A hung FULL owner (serving; the controller must kill it).
    hung = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "never"), "clean")
    hung_record = hung.result()
    assert hung_record["started"] is True

    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True
    assert contender.wait() == harness.EXIT_BUSY

    hung.terminate()
    deadline = time.monotonic() + 15
    while hung.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert hung.poll() is not None, "the OS terminated the hung full owner"

    phases_path = scratch / "phases.ndjson"
    successor = harness.start_worker(
        scratch, "full-owner-observing", state_dir, harness.gate(scratch, "stop"), phases_path
    )
    try:
        record = successor.result()
        assert record["started"] is True, record
        deadline = time.monotonic() + 10
        while (
            not phases_path.exists() or "ready" not in phases_path.read_text(encoding="utf-8")
        ) and time.monotonic() < deadline:
            time.sleep(0.05)
        phases = [
            json.loads(line)["phase"]
            for line in phases_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        # F-110: the law is observed at SessionManager.start() ENTRY —
        # the browser/session boundary — not at the later M5-stack
        # install. A regression that launched a real browser before
        # hydration would surface as browser-start-called preceding
        # hydrated.
        assert phases[0] == "acquiring", phases
        assert "hydrated" in phases and "browser-start-called" in phases, phases
        assert phases.index("hydrated") < phases.index("browser-start-called"), (
            f"hydration precedes the browser/session start: {phases}"
        )
        assert phases.index("browser-start-called") < phases.index("ready"), phases
        assert phases[-1] == "ready", phases
    finally:
        harness.open_gate(scratch, "stop")
        successor.wait()


# ---------------------------------------------------------------------------
# T38: crash DURING a reconciliation append — the M6 tail semantics hold
# ---------------------------------------------------------------------------


def _seed_unresolved_effect(state_dir: Path) -> str:
    """Seed one durable EFFECT_UNKNOWN effect so a reconciliation resolve
    has real work; returns the effect id."""
    from webwire.config import WebWireConfig
    from webwire.safety.effect_ledger import EffectLedger, EffectLedgerRecord, EffectState

    cfg = WebWireConfig(state_dir=state_dir, kill_env_var=None)
    ledger = EffectLedger(cfg)
    record = EffectLedgerRecord(
        effect_id="fx-t38",
        semantic_key="actor|like|post|t38|",
        state=EffectState.EFFECT_UNKNOWN,
        action_type="like",
        intent_hash="intent-t38",
        policy_binding="policy-t38",
        actor_id="actor",
        target_type="post",
        target_id="t38",
        timestamp="2026-10-08T00:00:00+00:00",
    )
    ledger.append_durable(record)
    return record.effect_id


def test_T38_torn_reconciliation_append_successor_semantics_hold(tmp_path: Path) -> None:
    """A real reconciliation resolve over IPC crashes MID-APPEND: the
    reconciliation ledger's final line is TORN (half the JSON, no
    newline — the on-disk shape a mid-append death leaves). The
    successor must exhibit exactly what the EXISTING M6 semantics
    dictate for that state, fail-closed or tolerated, with no new
    behavior introduced by the process boundary."""
    scratch = _scratch(tmp_path, "t38")
    state_dir = scratch / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    _seed_unresolved_effect(state_dir)

    owner = harness.start_worker(scratch, "recon-append-crash", state_dir, harness.gate(scratch, "die"))
    record = owner.result()
    assert record["started"] is True, record

    evidence = {
        "basis": "operator-review",
        "observed_at": "2026-10-08T00:00:00+00:00",
        "observations": [{"kind": "operator", "value": "verified"}],
    }
    opened = _request(
        scratch,
        record["endpoint"],
        record["build_id"],
        record["instance_id"],
        "reconciliation_open",
        {"operator_id": "op"},
    )
    wire_id = opened.result()["response"]["data"]["reconciliation_session_id"]
    prepared = _request(
        scratch,
        record["endpoint"],
        record["build_id"],
        record["instance_id"],
        "reconciliation_prepare",
        {
            "reconciliation_session_id": wire_id,
            "effect_id": "fx-t38",
            "verdict": "CONFIRMED_EFFECT",
            "evidence": evidence,
            "evidence_summary": "verified",
        },
    )
    proposal = prepared.result()["response"]["data"]["proposal"]
    confirmed = _request(
        scratch,
        record["endpoint"],
        record["build_id"],
        record["instance_id"],
        "reconciliation_confirm",
        {
            "reconciliation_session_id": wire_id,
            "proposal_id": proposal["proposal_id"],
            "confirmation_text": proposal["confirmation_text"],
        },
    )
    assert confirmed.result()["response"]["ok"] is True

    # The resolve fires the torn append; the owner dies uncleanly.
    resolver = _request(
        scratch,
        record["endpoint"],
        record["build_id"],
        record["instance_id"],
        "reconciliation_resolve",
        {"reconciliation_session_id": wire_id, "proposal_id": proposal["proposal_id"]},
    )
    torn_marker = owner.result_path.with_suffix(".torn")
    deadline = time.monotonic() + 15
    while not torn_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert torn_marker.exists(), "the torn append fired"
    resolver.kill()  # its response can never exist

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    recon = state_dir / "reconciliations.ndjson"
    assert recon.exists()
    raw_bytes = recon.read_bytes()
    # F-111 evidence hardening: the torn append wrote NON-ZERO real bytes
    # (an empty file would not be a torn append) and no newline survived.
    assert raw_bytes, "the real syscall wrote non-zero production bytes"
    raw = raw_bytes.decode("utf-8")
    assert not raw.endswith("\n"), "the final line is torn (no trailing newline)"

    # F-111: the implementation is deterministic — the reconciliation
    # ledger's reader rejects every non-empty ledger lacking its final
    # newline (ReconciliationLedgerCorruptError) — so the successor MUST
    # refuse startup fail-closed with a corruption reason naming the
    # reconciliation truth. No sanctioned successful-start alternative
    # exists at this head; a tolerated branch would need to prove the
    # seeded unresolved effect remains unresolved.
    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "after"), "clean")
    successor_record = successor.result(timeout=30)
    try:
        assert successor_record["started"] is False, (
            f"the torn tail must refuse startup fail-closed: {successor_record}"
        )
        error = str(successor_record.get("error", "")).lower()
        assert any(
            w in error for w in ("ambig", "corrupt", "reconcil", "ledger", "hydrate", "torn", "newline")
        ), f"the refusal must name the reconciliation corruption: {successor_record}"
    finally:
        harness.open_gate(scratch, "after")
        successor.wait()
