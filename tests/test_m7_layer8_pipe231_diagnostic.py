"""Layer 8 — M-13 diagnostic: named-pipe 231 with SERVER-SIDE correlation
(INSTRUMENT, not acceptance; Windows-only).

Drives the real production endpoint (WindowsNamedPipeEndpoint bind +
accept loop creating next instances) against concurrent raw clients that
only CreateFileW-connect and close. Every instance creation, every
ConnectNamedPipe return, and every client connect attempt is timestamped
into one event log; each ERROR_PIPE_BUSY (231) occurrence is then
CORRELATED against the nearest instance-lifecycle events, testing the
accept-window hypothesis directly: did the failing CreateFileW land
between the previous instance's connection and the next instance's
creation, when zero listening instances existed?

Diagnostic taxonomy (frozen matrix M-13): OBSERVED AND CLASSIFIED /
NOT REPRODUCED / INCONCLUSIVE — all three are valid outcomes here; the
record is the deliverable. No retry is proposed by this instrument.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="the Windows named-pipe mechanism under diagnosis")

ROUNDS = 300  # per client thread; enough attempts to surface the intermittent 231


def _ms(ref, ts: float, *, before: bool):
    if ref is None:
        return None
    delta = (ts - ref) if before else (ref - ts)
    return round(delta * 1000, 3)


def test_M13_pipe231_diagnostic(tmp_path: Path) -> None:
    from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint, _WindowsPipeConnection

    events: list[tuple[float, str, str]] = []
    lock = threading.Lock()

    def log(kind: str, detail: str = "") -> None:
        with lock:
            events.append((time.time(), kind, detail))

    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    original_create = endpoint._create_instance
    original_accept = endpoint.accept

    def _instrumented_create(*, first: bool):
        handle = original_create(first=first)
        if not first:
            with lock:
                progress["successor_instances"] += 1
        log("instance_created", "first" if first else "next")
        return handle

    def _instrumented_accept():
        log("connect_named_pipe_wait")
        conn = original_accept()
        log("connect_named_pipe_returned")
        return conn

    endpoint._create_instance = _instrumented_create  # type: ignore[method-assign]
    endpoint.accept = _instrumented_accept  # type: ignore[method-assign]
    endpoint.bind()

    stop = threading.Event()
    progress = {"accepts": 0, "successor_instances": 0, "server_errors": []}

    def server_loop() -> None:
        while not stop.is_set():
            try:
                conn = endpoint.accept()
                conn.close()
                with lock:
                    progress["accepts"] += 1
                log("accepted_closed")
            except Exception as exc:  # noqa: BLE001 - record and continue
                with lock:
                    progress["server_errors"].append(repr(exc)[:80])
                log("server_error", repr(exc)[:80])
                return

    def client_loop(tag: str) -> None:
        for _ in range(ROUNDS):
            try:
                client = _WindowsPipeConnection.open_client(endpoint.path)
                client.close()
                log("client_ok", tag)
            except Exception as exc:  # noqa: BLE001 - the record is the point
                log("client_fail", f"{tag}:{exc}"[:120])
            time.sleep(0.002)

    server = threading.Thread(target=server_loop, daemon=True)
    server.start()
    c1 = threading.Thread(target=client_loop, args=("c1",), daemon=True)
    c2 = threading.Thread(target=client_loop, args=("c2",), daemon=True)
    c1.start()
    c2.start()
    c1.join(timeout=120)
    c2.join(timeout=120)
    stop.set()
    try:
        endpoint.close()
    except Exception:  # noqa: BLE001 - shutdown hygiene only
        pass
    server.join(timeout=5)

    failures = [e for e in events if e[1] == "client_fail" and "231" in e[2]]
    other_failures = [e for e in events if e[1] == "client_fail" and "231" not in e[2]]
    clean_attempts = sum(1 for e in events if e[1] == "client_ok")
    alive = [c1.is_alive(), c2.is_alive()].count(True)
    completed_threads = 2 - alive

    record: dict[str, object] = {
        "intended_attempts": 2 * ROUNDS,
        "completed_client_threads": completed_threads,
        "clean": clean_attempts,
        "pipe_busy_231": len(failures),
        "other_failures": [f[2] for f in other_failures],
        "attempts": clean_attempts + len(failures) + len(other_failures),
    }

    # Correlate every 231 against the nearest instance-lifecycle events.
    instances = [e for e in events if e[1] == "instance_created"]
    windows: list[dict[str, object]] = []  # event samples only; the retired proximity rule is NOT applied
    for ts, _, detail in failures:
        prev_created = max((t for t, k, _ in instances if t <= ts), default=None)
        next_created = min((t for t, k, _ in instances if t > ts), default=None)
        windows.append(
            {
                "ts": ts,
                "client": detail.split(":")[0],
                "ms_since_prev_instance": _ms(prev_created, ts, before=True),
                "ms_until_next_instance": _ms(next_created, ts, before=False),
                "near_instance_creation_within_50ms": (
                    prev_created is not None and (ts - prev_created) < 0.05
                ),  # NOT causal (retired rule; samples only)
            }
        )
    record["failure_event_samples"] = windows[:20]
    record["stress_status"] = (
        "FAILURES_OBSERVED_SEE_SERVER_PROGRESS" if failures else "NOT_REPRODUCED"
    )  # causal attribution lives ONLY in the controlled experiment

    record["server_progress"] = {
        **progress,
        "server_thread_alive_after": server.is_alive(),
        # Longest gap with NO listening instance (successor creation is
        # the only path back to availability): longest stretch between
        # consecutive successor creations while clients were failing.
        "event_count": len(events),
    }
    saw_232 = any("232" in e for e in progress["server_errors"])
    if saw_232:
        # Full chronological stream for the 232 investigation (audit trail).
        record["events_full"] = [
            f"{ts:.6f} {kind} {detail}".strip() for ts, kind, detail in events
        ]
    else:
        record["events_head"] = [
            f"{ts:.6f} {kind} {detail}".strip() for ts, kind, detail in events[:30]
        ]
        record["events_tail"] = [
            f"{ts:.6f} {kind} {detail}".strip() for ts, kind, detail in events[-30:]
        ]
    print("M13-DIAG:", json.dumps(record, default=str))
    (tmp_path / "m13-diag.json").write_text(json.dumps(record, default=str, indent=2), encoding="utf-8")

    # Instrument invariants only (M-13-F02: exact accounting).
    assert completed_threads == 2, "both client threads must complete their rounds"
    assert record["attempts"] == 2 * ROUNDS, "every attempted connection is accounted"


def test_M13_controlled_accept_gap_falsification(tmp_path: Path) -> None:
    """The review-prescribed decisive experiment. Through the production
    accept() path, the _create_instance seam is held at the successor
    creation (E4) — the suspected window is DELIBERATELY open. Client B
    then attempts CreateFileW (E5). If B receives 231 and connectivity
    resumes after the barrier releases (E7/E8), the gap demonstrably
    causes the observed failure: DEMONSTRATED, not correlated. If B
    connects during the held gap, the hypothesis is FALSIFIED — recorded
    honestly either way. Event ordering uses perf_counter_ns."""
    import threading

    from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint, _WindowsPipeConnection

    order: list[tuple[int, str]] = []
    olock = threading.Lock()

    def mark(event: str) -> None:
        with olock:
            order.append((time.perf_counter_ns(), event))

    endpoint = WindowsNamedPipeEndpoint(tmp_path)
    original_create = endpoint._create_instance
    barrier = threading.Event()
    reached = threading.Event()

    def _gated_create(*, first: bool):
        if first:
            handle = original_create(first=True)
            mark("E1_first_instance_created")
            return handle
        mark("E3_accept_reached_successor_creation")
        reached.set()
        assert barrier.wait(timeout=10), "barrier never released"
        mark("E7a_barrier_released")
        mark("E7b_successor_creation_started")
        handle = original_create(first=False)
        mark("E7c_successor_creation_completed")
        return handle

    endpoint._create_instance = _gated_create  # type: ignore[method-assign]
    endpoint.bind()  # E1 happens here

    def server() -> None:
        conn = endpoint.accept()  # blocks in ConnectNamedPipe until A connects
        conn.close()

    srv = threading.Thread(target=server, daemon=True)
    srv.start()
    time.sleep(0.2)

    # E2: client A connects to the listening first instance.
    a = _WindowsPipeConnection.open_client(endpoint.path)
    mark("E2_client_A_connected")
    a.close()

    # E3: production accept() must reach successor creation (ConnectNamedPipe done).
    assert reached.wait(timeout=5), "accept never reached successor creation"
    mark("E4_gap_open_barrier_holding")

    # E5/E6: client B attempts CreateFileW while the gap is open.
    b_outcome: dict[str, object] = {}
    try:
        b = _WindowsPipeConnection.open_client(endpoint.path)
        b_outcome["connected"] = True
        b.close()
    except Exception as exc:  # noqa: BLE001 - the record is the point
        b_outcome["connected"] = False
        b_outcome["error"] = repr(exc)
    mark("E6_client_B_attempt_finished")

    # E7: release; the successor instance is created; accept returns.
    barrier.set()
    srv.join(timeout=10)
    time.sleep(0.2)

    # E8: a new client connects successfully.
    try:
        c = _WindowsPipeConnection.open_client(endpoint.path)
        e8 = True
        c.close()
    except Exception:  # noqa: BLE001
        e8 = False
    mark("E8_new_client_attempt_finished")
    try:
        endpoint.close()
    except Exception:  # noqa: BLE001 - shutdown hygiene
        pass

    record = {
        "events": [{"ns": ns, "event": name} for ns, name in order],
        "client_B": b_outcome,
        "E8_new_client_connected": e8,
        "status": (
            "DEMONSTRATED"
            if (
                not b_outcome.get("connected")
                and "231" in str(b_outcome.get("error", ""))
                and e8
            )
            else "FALSIFIED_OR_ANOMALOUS"
        ),
    }
    print("M13-GAP-EXPERIMENT:", json.dumps(record, default=str))

    # Ordering invariants: the gap opened before B attempted, and the
    # successor was created only after the barrier released.
    names = [n for _, n in order]
    i4 = names.index("E4_gap_open_barrier_holding")
    i6 = names.index("E6_client_B_attempt_finished")
    i7a = names.index("E7a_barrier_released")
    i7c = names.index("E7c_successor_creation_completed")
    assert i4 < i6 < i7a < i7c, "the gap closed only after B attempted"


def test_M13_F04_client_disconnect_listener_termination(tmp_path: Path) -> None:
    """M-13-F04, rev 2 (F04-F01/F02 corrections): TWO arms around the real
    endpoint with production-semantics accept loops (break on
    OSError/RuntimeError — exactly _accept_loop's behavior).

    ARM 1 — the DECISIVE pre-accept ordering (F04-F01): the client
    connects and closes BEFORE the server ever calls accept(); the real
    accept() is invoked only afterward. If ConnectNamedPipe returns 232
    on the already-disconnected instance, the loop dies mid-traffic.

    ARM 2 — the sustained quick-close pattern from the prior rounds,
    retained for accumulation.

    Both arms (F04-F02): when a mid-traffic listener death is detected,
    the post-death client connection is attempted BEFORE any teardown,
    so unavailability is attributable to the death, not the test's own
    shutdown. Diagnostic only; no production change."""
    import threading

    from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint, _WindowsPipeConnection

    rounds = []

    def run_round(i: int, pre_accept_disconnect: bool) -> None:
        rnd_dir = tmp_path / f"r{i}"
        rnd_dir.mkdir()
        endpoint = WindowsNamedPipeEndpoint(rnd_dir)
        endpoint.bind()
        stop = threading.Event()
        arm = "pre_accept" if pre_accept_disconnect else "quick_close"
        outcome: dict[str, object] = {"round": i, "arm": arm}

        def production_semantics_loop(ep=endpoint, out=outcome, stop_flag=stop) -> None:
            while not stop_flag.is_set():
                try:
                    conn = ep.accept()
                    conn.close()
                    out["accepts"] = out.get("accepts", 0) + 1
                except (OSError, RuntimeError) as exc:
                    out["loop_error"] = repr(exc)[:100]
                    return  # production breaks

        if pre_accept_disconnect:
            # F04-F01: connect AND close BEFORE any accept() call — the
            # server's ConnectNamedPipe will face an already-gone client.
            try:
                quick = _WindowsPipeConnection.open_client(endpoint.path)
                quick.close()
                outcome["pre_accept_client"] = "connected_and_closed"
            except Exception as exc:  # noqa: BLE001
                outcome["pre_accept_client"] = repr(exc)[:80]
            srv = threading.Thread(target=production_semantics_loop, daemon=True)
            srv.start()
        else:
            srv = threading.Thread(target=production_semantics_loop, daemon=True)
            srv.start()
            time.sleep(0.1)
            try:
                quick = _WindowsPipeConnection.open_client(endpoint.path)
                quick.close()
                outcome["quick_client_connected"] = True
            except Exception as exc:  # noqa: BLE001
                outcome["quick_client_connected"] = repr(exc)[:80]

        time.sleep(0.15)
        died_mid_traffic = not srv.is_alive()
        outcome["listener_died_mid_traffic"] = died_mid_traffic
        if died_mid_traffic and "loop_error" in outcome:
            # F04-F02: probe reachability BEFORE any teardown — a failure
            # here is attributable to the listener death alone.
            try:
                late = _WindowsPipeConnection.open_client(endpoint.path)
                outcome["post_death_client_before_teardown"] = "connected"
                late.close()
            except Exception as exc:  # noqa: BLE001
                outcome["post_death_client_before_teardown"] = repr(exc)[:80]
        stop.set()
        try:
            endpoint.close()
        except Exception:  # noqa: BLE001 - hygiene
            pass
        srv.join(timeout=5)
        outcome["listener_alive_after_shutdown"] = srv.is_alive()
        rounds.append(outcome)

    for i in range(10):
        run_round(i, pre_accept_disconnect=True)
    for i in range(10, 20):
        run_round(i, pre_accept_disconnect=False)

    reproduced = [
        r
        for r in rounds
        if r.get("loop_error") and "232" in str(r["loop_error"]) and r.get("listener_died_mid_traffic")
    ]
    pre_accept_232 = [r for r in reproduced if r.get("arm") == "pre_accept"]
    record = {
        "rounds": rounds,
        "error_232_loop_deaths": len(reproduced),
        "pre_accept_232_deaths": len(pre_accept_232),
        "status": (
            "REPRODUCED_PRE_ACCEPT" if pre_accept_232
            else "REPRODUCED_UNDER_LOAD" if reproduced
            else "NOT_REPRODUCED"
        ),
    }
    print("M13-F04-REPRO:", json.dumps(record, default=str))
    (tmp_path / "f04.json").write_text(json.dumps(record, default=str, indent=2), encoding="utf-8")
    assert len(rounds) == 20
