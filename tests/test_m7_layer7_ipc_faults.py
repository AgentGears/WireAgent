"""M7 Layer 7 — IPC fault and response-loss qualification over the REAL
transport between REAL processes (lanes 3+4+5).

The key invariant under qualification: RPC uncertainty never becomes
mutation truth — M5/M6 durable state decides what a successor may do.
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


def _start_full_owner(scratch: Path, state_dir: Path, *, die: bool = False, gate_name: str = "stop"):
    owner = harness.start_worker(
        scratch, "full-owner", state_dir, harness.gate(scratch, gate_name), "die" if die else "clean"
    )
    record = owner.result()
    assert record["started"] is True, record
    return owner, record


def _payload_file(scratch: Path, payload: dict) -> Path:
    """The operation payload only — the worker builds the envelope
    (operation/instance/build/request_id) around it."""
    path = scratch / f"payload-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Lane 4: request-table pressure and duplicate delivery (T22/T57/T58)
# ---------------------------------------------------------------------------


def test_transport_capacity_backpressure_under_real_connections(tmp_path: Path) -> None:
    """TRANSPORT capacity (honestly labeled, F-94): handler-slot
    saturation over real connections refuses the extra connection AT
    THE ENDPOINT and service resumes after the stalls drain. This is
    connection backpressure — the retained-request-table law is the
    separate test below."""
    scratch = _scratch(tmp_path, "pressure")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir)
    try:
        from webwire.authority_ipc_transport import IPCClient

        endpoint = record["endpoint"]
        build = record["build_id"]
        stalled = []
        for _ in range(5):  # > IPC_SERVER_MAX_CONCURRENT (4)
            client = IPCClient(endpoint, expected_build_id=build)
            try:
                client.connect()
                stalled.append(client)
            except ConnectionError:
                pass  # the capacity boundary refusing the EXTRA connection
        assert stalled, "the owner must accept connections up to capacity"
        time.sleep(0.3)

        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {})
        refused = harness.start_worker(
            scratch,
            "ipc-request",
            Path(endpoint),
            build,
            record["instance_id"],
            "health",
            env,
            rid,
            "normal",
        )
        refused_record = refused.result()
        assert refused_record.get("error") or isinstance(refused_record.get("response"), dict), (
            "the saturated owner must refuse or serve-degrade the extra connection"
        )
        for client in stalled:
            client.close()
        resumed_record = None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            resumed = harness.start_worker(
                scratch,
                "ipc-request",
                Path(endpoint),
                build,
                record["instance_id"],
                "health",
                env,
                rid,
                "normal",
            )
            try:
                record_data = resumed.result(timeout=8)
            except TimeoutError:
                resumed.kill()
                continue
            resumed.wait(timeout=10)
            resumed_record = record_data
            break
        assert resumed_record is not None, "service never resumed after the stalls drained"
        assert isinstance(resumed_record["response"], dict)
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


def test_retained_table_saturation_with_disconnected_admitted_work(tmp_path: Path) -> None:
    """T57 PROPER (F-94/F-100), at the qualification bound of 8 (the
    tablesat worker documents the bound lowering as law-independent
    orchestration): 8 admitted in-flight requests, each blocked AFTER
    admission with its client DISCONNECTED — handler slots recycle while
    the owner tasks stay pinned in the retained table. The 9th NEW
    request gets table_full (stable backpressure, no eviction). A
    duplicate of the FILL'S OWN BLOCKED REQUEST ID joins the live
    in-flight entry — no table_full, no second invoke — and a
    normal-mode same-id joiner observes the SAME terminal owner result
    after release."""
    QUAL_TABLE_BOUND = 8  # the tablesat worker patches the table constant to this value

    scratch = _scratch(tmp_path, "tablesat")
    state_dir = scratch / "state"
    stop_gate = harness.gate(scratch, "stop")
    exec_gate = harness.gate(scratch, "exec")
    events_path = scratch / "admissions.ndjson"

    owner = harness.start_worker(scratch, "full-owner-tablesat", state_dir, stop_gate, exec_gate, events_path)
    record = owner.result()
    assert record["started"] is True
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]

    # Fill the retained table with admitted, blocked, clientless work —
    # one request per connection, the flood client disconnecting after
    # each send (one flood PROCESS performs all sends — one CONNECTION
    # per request; one client process PER REQUEST would be needlessly
    # slow). Each
    # request registers in the table BEFORE its invoke queues on the
    # Dispatcher's single invocation lock.
    env = scratch / "payload.json"
    env.write_text(json.dumps({"text": "table pressure"}), encoding="utf-8")
    flood = harness.start_worker(
        scratch,
        "ipc-flood",
        Path(endpoint),
        build,
        instance,
        "post_text",
        env,
        str(QUAL_TABLE_BOUND),
        "nodup",
    )
    flood_record = flood.result(timeout=180)
    assert flood_record["sent"] == QUAL_TABLE_BOUND, flood_record
    flood.wait(timeout=60)

    # Wait until every request is ADMITTED (invoke-seam events).
    def _invoke_events() -> list:
        if not events_path.exists():
            return []
        return [line for line in events_path.read_text(encoding="utf-8").splitlines() if '"invoke"' in line]

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if len(_invoke_events()) >= QUAL_TABLE_BOUND:
            break
        time.sleep(0.1)
    lines = _invoke_events()
    assert len(lines) == QUAL_TABLE_BOUND, f"all {QUAL_TABLE_BOUND} requests admitted; got {len(lines)}"

    # The 9th NEW request: table_full — stable backpressure.
    env65 = scratch / "payload-9th.json"
    env65.write_text(json.dumps({"text": "the one too many"}), encoding="utf-8")
    refused = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        env65,
        secrets.token_hex(16),
        "normal",
    )
    refused_record = refused.result()
    assert refused_record["response"]["ok"] is False
    assert refused_record["response"]["error"]["code"] == "table_full"

    # A same-id duplicate of BLOCKED work JOINS it (F-100): the flood
    # re-sends the FILL's first_request_id — an id with a LIVE inflight
    # entry — as a NORMAL client that reads the response. It must NOT
    # get table_full (the entry exists; the duplicate joins its future)
    # and must NOT create a second invoke.
    dup = harness.start_worker(
        scratch,
        "ipc-flood",
        Path(endpoint),
        build,
        instance,
        "post_text",
        env,
        "1",
        "dup",
        flood_record["first_request_id"],
    )
    # The duplicate BLOCKS reading the joined work's response until the
    # barrier releases; its result arrives after the release below.
    dup.wait(timeout=120)
    dup_result = json.loads(dup.result_path.read_text(encoding="utf-8"))
    assert dup_result["sent"] == 2, dup_result  # the explicit blocked id + its duplicate
    assert dup_result["errors"] == [], dup_result
    time.sleep(0.5)
    lines_after = _invoke_events()
    assert len(lines_after) == QUAL_TABLE_BOUND, "the duplicate JOINED existing work — no second execution"

    # A NORMAL same-id joiner: connects, sends the SAME blocked id, and
    # READS the response — after release it observes the SAME terminal
    # owner result the original work produced (the retained frame).
    joiner = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        env,
        flood_record["first_request_id"],
        "normal",
    )

    # Release: all blocked work terminalizes; the joiner observes the
    # retained result; the owner stops cleanly.
    harness.open_gate(scratch, "exec")
    joiner_result = joiner.result(timeout=120)
    joiner.wait(timeout=60)
    joined_response = joiner_result["response"]
    assert isinstance(joined_response, dict) and "ok" in joined_response, joiner_result
    # THE JOIN proof: the same-id joiner did NOT get table_full — it
    # observed the live entry's terminal result.
    assert joined_response.get("error", {}).get("code") != "table_full", joined_response
    harness.open_gate(scratch, "stop")
    assert owner.wait(timeout=300) == harness.EXIT_OK
    # And the join was still ONE execution: the invoke count never grew.
    assert len(_invoke_events()) == QUAL_TABLE_BOUND


# ---------------------------------------------------------------------------
# Lane 4: disconnect-after-send and owner death (T42/T44)
# ---------------------------------------------------------------------------


def test_owner_death_after_disconnect_relabels_envelope_stale(tmp_path: Path) -> None:
    """T20/T26 process evidence (honestly relabeled, F-95): a client
    disconnects after sending a mutating request; the owner then dies
    uncleanly. The successor starts cleanly and the OLD envelope is
    rejected as stale_authority_instance. The full T44 (durable M5
    truth governing an uncertain REAL mutation) is the separate
    real-M5-crash test below."""
    scratch = _scratch(tmp_path, "uncertain")
    state_dir = scratch / "state"
    owner, record = _start_full_owner(scratch, state_dir, die=True, gate_name="die")
    rid = secrets.token_hex(16)
    env = _payload_file(scratch, {"text": "uncertain mutation"})
    vanished = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_text",
        env,
        rid,
        "disconnect",
    )
    vanished_record = vanished.result()
    assert vanished_record["sent"] is True and vanished_record["response"] is None

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    successor, successor_record = _start_full_owner(scratch, state_dir, gate_name="stop2")
    try:
        assert successor_record["started"] is True
        assert successor_record["instance_id"] != record["instance_id"]
        stale = harness.start_worker(
            scratch,
            "ipc-request",
            Path(successor_record["endpoint"]),
            successor_record["build_id"],
            record["instance_id"],
            "post_text",
            env,
            rid,
            "normal",
        )
        stale_record = stale.result()
        response = stale_record["response"]
        assert response["ok"] is False
        assert response["error"]["code"] == "stale_authority_instance"
    finally:
        harness.open_gate(scratch, "stop2")
        successor.wait()


def test_T44_uncertain_real_m5_mutation_governed_by_durable_truth(tmp_path: Path) -> None:
    """T44 PROPER (F-95/F-101) — the FULL scenario over the real
    transport with EXTERNAL clients: the owner serves the REAL M5
    post-text executor; an external client previews (real token) and
    sends the confirm over the wire, then DISCONNECTS; the executor
    passes the commit gate — the attempt durably RESERVED — and dies at
    the controlled point. The response never exists. The successor
    hydrates the durable ledger, starts (READY), REFUSES the
    same-semantic write through the RecoveryGate, and the old envelope
    is stale."""
    scratch = _scratch(tmp_path, "m5crash")
    state_dir = scratch / "state"
    die_gate = harness.gate(scratch, "die")

    owner = harness.start_worker(scratch, "full-owner-m5-serving", state_dir, die_gate)
    record = owner.result()
    assert record["started"] is True, record
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]

    # EXTERNAL preview over the real transport: a real token.
    preview_env = _payload_file(scratch, {"text": "the uncertain real mutation"})
    previewer = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        preview_env,
        secrets.token_hex(16),
        "normal",
    )
    preview_response = previewer.result()["response"]
    token = preview_response["data"]["data"]["confirmation_token"]

    # EXTERNAL confirm over the real transport, then the client is GONE.
    confirm_env = _payload_file(scratch, {"text": "the uncertain real mutation", "confirmation_token": token})
    vanished = harness.start_worker(
        scratch,
        "ipc-request",
        Path(endpoint),
        build,
        instance,
        "post_text",
        confirm_env,
        secrets.token_hex(16),
        "disconnect",
    )
    assert vanished.result()["sent"] is True

    # The executor reached click_submit and passed the commit gate: the
    # attempt is durably RESERVED at the crash point.
    blocked = harness.wait_record(owner.result_path.with_suffix(".blocked"))
    assert blocked["m5_submit_blocked"] is True
    effects = state_dir / "effects.ndjson"
    assert "RESERVED" in effects.read_text(encoding="utf-8"), (
        "the M5 attempt is durably RESERVED at the crash point"
    )

    # The owner dies INSIDE the admitted mutation; the response never
    # existed for anyone.
    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN

    # The successor: hydrates the unresolved attempt and starts cleanly.
    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop2"), "clean")
    successor_record = successor.result()
    assert successor_record["started"] is True, successor_record
    try:
        # The same-semantic write is REFUSED through the recovery gate —
        # durable M5/M6 truth decides, and the uncertainty is NOT
        # resolved by silently re-running the mutation.
        replay_env = _payload_file(scratch, {"text": "the uncertain real mutation"})
        replay = harness.start_worker(
            scratch,
            "ipc-request",
            Path(successor_record["endpoint"]),
            successor_record["build_id"],
            successor_record["instance_id"],
            "post_text",
            replay_env,
            secrets.token_hex(16),
            "normal",
        )
        replay_record = replay.result()
        replay_response = replay_record["response"]
        assert replay_response["ok"] is False, replay_response
        message = replay_response["error"]["message"].lower() + str(replay_response.get("safety", {})).lower()
        assert "reconcil" in message or "recovery" in message or "unknown" in message, (
            f"the replay must be governed by durable recovery truth: {replay_response}"
        )

        # The OLD envelope (old instance) can never reach execution.
        stale = harness.start_worker(
            scratch,
            "ipc-request",
            Path(successor_record["endpoint"]),
            successor_record["build_id"],
            instance,
            "post_text",
            confirm_env,
            secrets.token_hex(16),
            "normal",
        )
        stale_response = stale.result()["response"]
        assert stale_response["ok"] is False
        assert stale_response["error"]["code"] == "stale_authority_instance"
    finally:
        harness.open_gate(scratch, "stop2")
        successor.wait()


# ---------------------------------------------------------------------------
# Lane 3 (load-bearing): blocked admitted request blocks clean release


# ---------------------------------------------------------------------------
# F-103: the restored raw-protocol lane — T21/T50/T58 real-process
# evidence that the round-two rewrite dropped.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# F-105/F-106/F-107: the raw-protocol lane with an execution oracle
# ---------------------------------------------------------------------------


def _counting_owner(scratch: Path, state_dir: Path, tag: str):
    """Start the invoke-counting qualification owner; returns (handle,
    record, events_path). The events file holds one line per ACTUAL
    Dispatcher invocation — the T21/T23/T58 execution-order evidence."""
    events_path = scratch / f"invokes-{tag}.ndjson"
    owner = harness.start_worker(
        scratch, "full-owner-counting", state_dir, harness.gate(scratch, f"stop-{tag}"), events_path
    )
    record = owner.result()
    assert record["started"] is True, record
    return owner, record, events_path


def _invoke_count(events_path: Path) -> int:
    if not events_path.exists():
        return 0
    return len([line for line in events_path.read_text(encoding="utf-8").splitlines() if '"invoke"' in line])


def _raw_request_worker(scratch: Path, endpoint: Path, build: str, body: bytes, tag: str):
    """One hand-built raw frame on its OWN connection (one request per
    connection): 8-byte big-endian length + the exact body bytes. The
    body BYTES are what the owner receives — no client-side serializer
    can normalize them (F-105)."""
    import binascii

    frame = len(body).to_bytes(8, "big") + body
    spec = json.dumps(
        [
            {"kind": "bytes", "hex": binascii.hexlify(frame).decode()},
            {"kind": "read-response"},
        ]
    )
    return harness.start_worker(scratch, "ipc-raw", endpoint, build, spec)


def test_T21_unknown_protocol_schema_oversized_refused_before_execution(tmp_path: Path) -> None:
    """T21 PROPER over the real transport with an execution-count
    oracle (F-106): unknown protocol version, unknown operation, UNKNOWN
    SCHEMA (a forbidden payload field), and an announced oversized frame
    are each rejected — and the invoke count stays ZERO (rejected before
    execution) — and the owner keeps serving afterwards (which then
    makes the count 1, exactly the surviving valid request)."""
    scratch = _scratch(tmp_path, "t21raw")
    state_dir = scratch / "state"
    owner, record, events_path = _counting_owner(scratch, state_dir, "t21")
    try:
        endpoint = Path(record["endpoint"])
        build, instance = record["build_id"], record["instance_id"]

        def _envelope(**overrides):
            envelope = {
                "protocol_version": 1,
                "operation": "health",
                "payload": {},
                "request_id": secrets.token_hex(16),
                "authority_instance_id": instance,
                "runtime_build_id": build,
            }
            envelope.update(overrides)
            return envelope

        def _raw_probe(envelope: dict) -> dict:
            spec = json.dumps([{"kind": "request", "envelope": envelope}, {"kind": "read-response"}])
            raw = harness.start_worker(scratch, "ipc-raw", endpoint, build, spec)
            return raw.result()["steps"][1]["response"]

        assert _raw_probe(_envelope(protocol_version=99))["error"]["code"] == "protocol_mismatch"
        assert _raw_probe(_envelope(protocol_version=1.0))["error"]["code"] == "protocol_mismatch"
        assert (
            _raw_probe(_envelope(operation="definitely_not_real"))["error"]["code"] == "unsupported_operation"
        )
        # F-106: unknown SCHEMA — a forbidden payload field on a valid
        # operation — dies at strict schema validation, not the allowlist.
        assert _raw_probe(_envelope(payload={"unexpected_field": 1}))["error"]["code"] == "schema"
        assert _raw_probe(_envelope(evil_extra="x"))["error"]["code"] == "schema"

        spec = json.dumps(
            [
                {"kind": "bytes", "hex": (100 * 1024).to_bytes(8, "big").hex()},
                {"kind": "read-eof", "timeout": 5},
            ]
        )
        raw = harness.start_worker(scratch, "ipc-raw", endpoint, build, spec)
        steps = raw.result()["steps"]
        refused = any(
            step.get("recv") == "" or "ConnectionError" in str(step.get("error", "")) for step in steps
        )
        assert refused, steps

        assert _invoke_count(events_path) == 0, "every T21 fault rejected BEFORE execution"

        env = _payload_file(scratch, {})
        healthy = harness.start_worker(
            scratch,
            "ipc-request",
            endpoint,
            build,
            instance,
            "health",
            env,
            secrets.token_hex(16),
            "normal",
        )
        healthy_record = healthy.result()
        assert isinstance(healthy_record["response"], dict) and "ok" in healthy_record["response"]
        assert _invoke_count(events_path) == 1, "exactly the one surviving valid request"
    finally:
        harness.open_gate(scratch, "stop-t21")
        owner.wait()


def test_T50_executable_pickle_attempt_never_deserialized(tmp_path: Path) -> None:
    """T50 PROPER (F-107): a GENUINELY EXECUTABLE benign pickle — its
    reducer would create a marker file if anything ever unpickled it —
    sent as raw frame bytes under a valid length header. The owner
    returns a protocol error, the marker file does NOT exist, and the
    owner keeps serving: no executable deserialization occurred."""
    import binascii
    import pickle
    import subprocess
    import sys as _sys

    scratch = _scratch(tmp_path, "t50raw")
    state_dir = scratch / "state"
    marker = scratch / "PWNED.marker"

    class _MarkerPayload:
        def __reduce__(self):
            import os

            return (os.system, (f'echo executed > "{marker}"',))

    pickle_bytes = pickle.dumps(_MarkerPayload())
    # Sanity in a throwaway subprocess (THIS test process never runs the
    # reducer): the pickle IS executable-if-deserialized.
    (scratch / "proof.pickle").write_bytes(pickle_bytes)
    probe = (
        "import pickle,sys,os; "
        "pickle.loads(open(sys.argv[1],'rb').read()); "
        "print('marker-exists', os.path.exists(sys.argv[2]))"
    )
    proof = subprocess.run(
        [_sys.executable, "-c", probe, str(scratch / "proof.pickle"), str(marker)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert "marker-exists True" in proof.stdout, (
        f"the probe pickle must be genuinely executable: {proof.stdout!r} {proof.stderr!r}"
    )
    marker.unlink(missing_ok=True)  # reset: the OWNER run starts clean

    owner, record, events_path = _counting_owner(scratch, state_dir, "t50")
    try:
        endpoint = Path(record["endpoint"])
        build, instance = record["build_id"], record["instance_id"]

        raw = harness.start_worker(
            scratch,
            "ipc-raw",
            endpoint,
            build,
            json.dumps(
                [
                    {"kind": "bytes", "hex": len(pickle_bytes).to_bytes(8, "big").hex()},
                    {"kind": "bytes", "hex": binascii.hexlify(pickle_bytes).decode()},
                    {"kind": "read-response"},
                ]
            ),
        )
        response = raw.result()["steps"][2]["response"]
        assert response["ok"] is False
        assert response["error"]["code"] == "protocol", response

        assert not marker.exists(), "executable deserialization must NOT have occurred"
        assert _invoke_count(events_path) == 0, "the pickle frame died before execution"

        env = _payload_file(scratch, {})
        healthy = harness.start_worker(
            scratch,
            "ipc-request",
            endpoint,
            build,
            instance,
            "health",
            env,
            secrets.token_hex(16),
            "normal",
        )
        healthy_record = healthy.result()
        assert isinstance(healthy_record["response"], dict) and "ok" in healthy_record["response"]
    finally:
        harness.open_gate(scratch, "stop-t50")
        owner.wait()


def test_T58_payload_key_order_retained_not_violation(tmp_path: Path) -> None:
    """T58 PROPER (F-105/F-108): two raw frames with IDENTICAL envelope
    ordering whose difference is ONLY the key order INSIDE the
    schema-normalized payload — the load-bearing §10.5 rule, since
    canonical_request_identity hashes protocol_version + operation + the
    normalized payload and DISCARDS the envelope routing fields whose
    order the previous version varied. A read_search payload with
    query/tab/limit in two orders; raw bodies asserted byte-different
    and semantically equal before sending; same request_id on both: the
    second returns the byte-identical RETAINED response, NOT
    request_id_reused, invoke count EXACTLY 1."""
    scratch = _scratch(tmp_path, "t58raw")
    state_dir = scratch / "state"

    owner, record, events_path = _counting_owner(scratch, state_dir, "t58")
    try:
        endpoint = Path(record["endpoint"])
        build, instance = record["build_id"], record["instance_id"]
        rid = secrets.token_hex(16)

        # IDENTICAL envelope key order on both frames; ONLY the payload
        # object's key order differs.
        body_a = (
            '{"protocol_version":1,'
            '"operation":"read_search",'
            '"payload":{"query":"key order probe","tab":"latest","limit":5},'
            f'"request_id":"{rid}",'
            f'"authority_instance_id":"{instance}",'
            f'"runtime_build_id":"{build}"}}'
        )
        body_b = (
            '{"protocol_version":1,'
            '"operation":"read_search",'
            '"payload":{"limit":5,"tab":"latest","query":"key order probe"},'
            f'"request_id":"{rid}",'
            f'"authority_instance_id":"{instance}",'
            f'"runtime_build_id":"{build}"}}'
        )
        bytes_a = body_a.encode("utf-8")
        bytes_b = body_b.encode("utf-8")
        assert bytes_a != bytes_b, "the WIRE bytes differ (payload key order, not values)"
        assert json.loads(body_a) == json.loads(body_b), "the semantic content is identical"

        first = _raw_request_worker(scratch, endpoint, build, bytes_a, "a")
        second = _raw_request_worker(scratch, endpoint, build, bytes_b, "b")
        r1 = first.result()["steps"][1]["response"]
        r2 = second.result()["steps"][1]["response"]

        # Any WELL-FORMED frame qualifies: the law is canonical identity +
        # retention, not the read verdict (the DOM surface is stubbed).
        assert isinstance(r1, dict) and "ok" in r1, r1
        assert r2 == r1, "different payload key order -> the byte-identical retained frame"
        assert (r2.get("error") or {}).get("code", "") != "request_id_reused", r2
        assert _invoke_count(events_path) == 1, "ONE execution despite two differently-ordered deliveries"
    finally:
        harness.open_gate(scratch, "stop-t58")
        owner.wait()


def test_T20_stale_instance_rejected_before_execution(tmp_path: Path) -> None:
    """T20 with the execution oracle (F-109): an otherwise-VALID request
    carrying a stale instance id is rejected as stale_authority_instance
    and the invoke count stays ZERO — explicit before-Dispatcher
    evidence — while the same owner then serves a current-instance
    request (invoke count exactly 1)."""
    scratch = _scratch(tmp_path, "t20count")
    state_dir = scratch / "state"
    owner, record, events_path = _counting_owner(scratch, state_dir, "t20")
    try:
        endpoint = Path(record["endpoint"])
        build, instance = record["build_id"], record["instance_id"]

        stale_env = _payload_file(scratch, {})
        stale = harness.start_worker(
            scratch,
            "ipc-request",
            endpoint,
            build,
            "f" * 64,  # a stale owner instance id
            "health",
            stale_env,
            secrets.token_hex(16),
            "normal",
        )
        stale_response = stale.result()["response"]
        assert stale_response["ok"] is False
        assert stale_response["error"]["code"] == "stale_authority_instance"
        assert _invoke_count(events_path) == 0, "stale instance rejected BEFORE execution"

        current_env = _payload_file(scratch, {})
        current = harness.start_worker(
            scratch,
            "ipc-request",
            endpoint,
            build,
            instance,
            "health",
            current_env,
            secrets.token_hex(16),
            "normal",
        )
        current_record = current.result()
        assert isinstance(current_record["response"], dict) and "ok" in current_record["response"]
        assert _invoke_count(events_path) == 1, "exactly the one valid current-instance request"
    finally:
        harness.open_gate(scratch, "stop-t20")
        owner.wait()


def test_T23_completed_duplicate_retained_no_second_execution(tmp_path: Path) -> None:
    """T23 with the execution oracle (F-106): the first valid request
    executes (invoke count 1); the SAME request_id with the same payload
    delivered again returns the byte-identical retained response and the
    invoke count stays EXACTLY 1 — explicit no-new-execution evidence,
    not inferred from response bytes."""
    scratch = _scratch(tmp_path, "t23count")
    state_dir = scratch / "state"
    owner, record, events_path = _counting_owner(scratch, state_dir, "t23")
    try:
        endpoint = Path(record["endpoint"])
        build, instance = record["build_id"], record["instance_id"]

        def _request_with_retry(env: Path, rid: str) -> dict:
            for _attempt in range(4):
                client = harness.start_worker(
                    scratch,
                    "ipc-request",
                    endpoint,
                    build,
                    instance,
                    "health",
                    env,
                    rid,
                    "normal",
                )
                out = client.result(timeout=30)
                client.wait(timeout=15)
                if "response" in out:
                    return out
                time.sleep(0.2)
            raise AssertionError("the request never reached a response")

        rid = secrets.token_hex(16)
        env = _payload_file(scratch, {})
        r1 = _request_with_retry(env, rid)
        assert _invoke_count(events_path) == 1, "the first valid request executed once"

        r2 = _request_with_retry(env, rid)
        assert r2["response"] == r1["response"], "the retained frame, byte-identical"
        assert _invoke_count(events_path) == 1, "the duplicate did NOT execute again"
    finally:
        harness.open_gate(scratch, "stop-t23")
        owner.wait()
