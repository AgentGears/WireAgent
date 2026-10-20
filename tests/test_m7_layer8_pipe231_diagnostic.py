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

    def server_loop() -> None:
        while not stop.is_set():
            try:
                conn = endpoint.accept()
                conn.close()
                log("accepted_closed")
            except Exception as exc:  # noqa: BLE001 - record and continue
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
    clean_attempts = sum(1 for e in events if e[1] == "client_ok")

    record: dict[str, object] = {
        "attempts": clean_attempts + len(failures),
        "clean": clean_attempts,
        "pipe_busy_231": len(failures),
    }

    # Correlate every 231 against the nearest instance-lifecycle events.
    instances = [e for e in events if e[1] == "instance_created"]
    windows: list[dict[str, object]] = []
    for ts, _, detail in failures:
        prev_created = max((t for t, k, _ in instances if t <= ts), default=None)
        next_created = min((t for t, k, _ in instances if t > ts), default=None)
        windows.append(
            {
                "ts": ts,
                "client": detail.split(":")[0],
                "ms_since_prev_instance": _ms(prev_created, ts, before=True),
                "ms_until_next_instance": _ms(next_created, ts, before=False),
                "in_accept_window": prev_created is not None and (ts - prev_created) < 0.05,
            }
        )
    record["correlations"] = windows[:20]
    if failures:
        in_window = sum(1 for w in windows if w["in_accept_window"])
        record["status"] = "OBSERVED" if in_window == len(failures) else "PARTIALLY_CORRELATED"
        record["in_accept_window_count"] = in_window
    else:
        record["status"] = "NOT_REPRODUCED"

    print("M13-DIAG:", json.dumps(record, default=str))
    (tmp_path / "m13-diag.json").write_text(json.dumps(record, default=str, indent=2), encoding="utf-8")

    # Instrument invariants only.
    assert record["attempts"] == record["clean"] + record["pipe_busy_231"]
    assert record["attempts"] > 0
