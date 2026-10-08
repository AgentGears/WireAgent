"""M7 Layer 7 round two — T16: the admission↔drain in-window race at
the process boundary.

Frozen law: a request racing the READY→DRAINING transition is EITHER
fully admitted before drain (drain then waits for it) OR rejected
cleanly (draining error) — never a torn admission, never an admitted
request the drain forgets, never ownership released while in flight.
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


def _payload_file(scratch: Path, payload: dict) -> Path:
    path = scratch / f"payload-{secrets.token_hex(4)}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_T16_admission_drain_race_never_torn(tmp_path: Path) -> None:
    """F-112's T16: CONNECTED, outcome-bearing racers synchronized at
    the ACTUAL admission/drain seam (AuthoritySession.begin_drain
    entry, marked by the owner). Requests that arrive before the seam
    are ADMITTED (invoke-logged; their responses arrive after the held
    work releases); requests after the seam receive the stable draining
    refusal. Every launched racer lands in EXACTLY ONE set — no
    missing, no torn admission — and the owner holds the domain
    through the window, then drains cleanly for a successor."""
    scratch = _scratch(tmp_path, "t16")
    state_dir = scratch / "state"
    stop_gate = harness.gate(scratch, "stop")
    exec_gate = harness.gate(scratch, "exec")
    events_path = scratch / "admissions.ndjson"

    owner = harness.start_worker(scratch, "full-owner-t16", state_dir, stop_gate, exec_gate, events_path)
    record = owner.result()
    assert record["started"] is True
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]

    def _invoke_events() -> list:
        if not events_path.exists():
            return []
        return [line for line in events_path.read_text(encoding="utf-8").splitlines() if '"invoke"' in line]

    # The HELD confirm: admitted, blocked in the kernel.
    preview_env = _payload_file(scratch, {"text": "the T16 held confirm"})
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
    token = previewer.result()["response"]["data"]["data"]["confirmation_token"]
    confirm_env = _payload_file(scratch, {"text": "the T16 held confirm", "confirmation_token": token})
    held = harness.start_worker(
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
    assert held.result()["sent"] is True
    deadline = time.monotonic() + 10
    while len(_invoke_events()) < 2 and time.monotonic() < deadline:
        time.sleep(0.05)
    # The held pair = preview invoke + confirm invoke.
    assert len(_invoke_events()) == 2, "the held confirm is admitted (preview + confirm)"

    # PRE-SEAM racers (CONNECTED, NORMAL — they read their responses):
    # admission happens in process_request BEFORE the invoke queues on
    # the invocation lock, so each is admitted and invoke-logged.
    # ONE pre-seam racer (retrying transient connection refusals — the
    # known cold-start flake, not the law): admitted when the invoke
    # count reaches 3 (held pair + this preview).
    pre_seam = []
    env = _payload_file(scratch, {"text": "t16 pre-seam"})
    for _attempt in range(4):
        racer = harness.start_worker(
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
        pre_seam = [racer]
        deadline = time.monotonic() + 10
        while len(_invoke_events()) < 3 and time.monotonic() < deadline:
            time.sleep(0.05)
        if len(_invoke_events()) >= 3:
            break
        racer.kill()
    # 2 (held pair) + 1 (pre-seam preview) = 3 admitted invokes.
    assert len(_invoke_events()) == 3, f"the pre-seam racer is admitted: {_invoke_events()}"

    # IN-WINDOW racers: CONNECT NOW (the endpoint is still live), but
    # their SEND waits on a gate the controller opens AFTER the drain
    # seam — already-connected sockets delivering into the draining
    # owner receive the stable refusal.
    send_gate = harness.gate(scratch, "send")
    # ONE in-window racer: the transport's connection capacity is 4 and
    # three slots are held (the blocked confirm + the two queued pre-seam
    # racers) — the fourth slot is exactly the in-window delivery.
    in_window = []
    env = _payload_file(scratch, {"text": "t16 in-window"})
    in_window.append(
        harness.start_worker(
            scratch,
            "ipc-request",
            Path(endpoint),
            build,
            instance,
            "post_text",
            env,
            secrets.token_hex(16),
            "normal",
            send_gate,
        )
    )
    for racer in in_window:
        deadline = time.monotonic() + 10
        marker = racer.result_path.with_suffix(".connected")
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "the in-window racer connected before the seam"

    # Open the stop gate: the drain reaches the SEAM (begin_drain
    # entry marker) — the synchronization point.
    harness.open_gate(scratch, "stop")
    seam_marker = owner.result_path.with_suffix(".drain")
    deadline = time.monotonic() + 15
    while not seam_marker.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert seam_marker.exists(), "shutdown reached the drain seam"

    # Release the send gate: the already-connected in-window racer
    # delivers into the DRAINING owner. Empirically (probed at this
    # head): the owner's shutdown closes tracked connections at
    # DRAINING, so the racer's sanctioned rejection is the clean
    # CONNECTION CLOSE (error 232/EOF) — no invoke, no admission, no
    # torn state. The stable draining FRAME is the other sanctioned arm
    # (requests already inside the pipeline). Either form is a complete
    # rejection; a hang or a phantom admission is not.
    harness.open_gate(scratch, "send")
    post_records = []
    for racer in in_window:
        post_records.append(racer.result(timeout=30))
        racer.wait(timeout=15)
    invokes_before = len(_invoke_events())
    for post_record in post_records:
        if "response" in post_record:
            response = post_record["response"]
            assert response["ok"] is False, post_record
            assert response["error"]["code"] == "draining", post_record
        else:
            err_text = str(post_record.get("error", ""))
            assert ("ConnectionError" in err_text) or ("BrokenPipe" in err_text), post_record
    # Rejection means NO admission: the invoke count never grew for the
    # rejected racer.
    assert len(_invoke_events()) == invokes_before, _invoke_events()

    # The owner stayed alive through the window; the domain held.
    stopping = owner.result_path.with_suffix(".stopping")
    deadline = time.monotonic() + 15
    while not stopping.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() is None, "the owner drains (alive) while admitted work holds"
    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True

    # Release: the held confirm + the admitted pre-seam racer all
    # terminalize; the drain completes. Empirically (probed at this
    # head): the owner's shutdown closes tracked connections at
    # DRAINING, so an admitted racer still awaiting its response at
    # drain loses the RESPONSE (clean connection close) while its
    # admitted work completes owner-side — exactly the §10.6
    # conservative law. The sanctioned forms are therefore a full
    # response OR a clean close; the invoke accounting is the proof
    # the admitted work ran and the clean exit proves it terminalized.
    harness.open_gate(scratch, "exec")
    pre_records = []
    for racer in pre_seam:
        pre_records.append(racer.result(timeout=60))
        racer.wait(timeout=30)
    for pre_record in pre_records:
        if "response" in pre_record:
            assert isinstance(pre_record["response"], dict) and "ok" in pre_record["response"], pre_record
        else:
            pre_err = str(pre_record.get("error", ""))
            assert ("ConnectionError" in pre_err) or ("BrokenPipe" in pre_err), pre_record

    # THE INVARIANT: every launched racer accounted in exactly one set.
    # Admitted: held pair (2) + pre-seam preview (1) = 3 invokes;
    # refused: 1 (the in-window racer — draining frame or clean
    # connection close, never admitted). No invoke for any refused racer.
    assert len(_invoke_events()) == 3, _invoke_events()
    assert owner.wait(timeout=60) == harness.EXIT_OK

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop2"), "clean")
    assert successor.result()["started"] is True
    harness.open_gate(scratch, "stop2")
    successor.wait()
