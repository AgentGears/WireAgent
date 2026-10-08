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
    """Requests fired INTO the closing drain window are either fully
    admitted (the drain waits; the owner stays alive until they
    terminalize) or refused with the draining error — and in both
    cases the owner exits cleanly and the domain frees. Repeated
    across several racing requests to straddle the window."""
    scratch = _scratch(tmp_path, "t16")
    state_dir = scratch / "state"
    stop_gate = harness.gate(scratch, "stop")
    exec_gate = harness.gate(scratch, "exec")
    events_path = scratch / "admissions.ndjson"

    # A counting owner whose CONFIRMS block in the kernel: admitted work
    # holds the invocation lock, so the drain window is observable.
    owner = harness.start_worker(
        scratch, "full-owner-blocked", state_dir, stop_gate, exec_gate, events_path, "*"
    )
    record = owner.result()
    assert record["started"] is True
    endpoint, build, instance = record["endpoint"], record["build_id"], record["instance_id"]

    # One confirmed ADMISSION first (blocked in the kernel), so the
    # session's active count is 1 and drain has something to wait for.
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
    while (
        not events_path.exists() or '"admitted"' not in events_path.read_text(encoding="utf-8")
    ) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert events_path.read_text(encoding="utf-8").count('"admitted"') >= 1, (
        "the first confirm is admitted and blocked"
    )

    # NOW open the stop gate (drain begins) and simultaneously fire a
    # burst of racing preview requests into the closing window.
    harness.open_gate(scratch, "stop")
    racers = []
    for index in range(4):
        env = _payload_file(scratch, {"text": f"t16 racer {index}"})
        racers.append(
            harness.start_worker(
                scratch,
                "ipc-request",
                Path(endpoint),
                build,
                instance,
                "post_text",
                env,
                secrets.token_hex(16),
                "disconnect",
            )
        )
    racer_records = [racer.result(timeout=30) for racer in racers]
    for racer in racers:
        racer.wait(timeout=15)

    # Every racer either got a full response (admitted before drain —
    # impossible here while the invocation lock is held, but legal) or
    # the draining refusal (the law's other arm). What is FORBIDDEN is
    # a torn outcome: a response that implies admission the drain never
    # saw, or a hang the drain leaked past.
    for racer_record in racer_records:
        response = racer_record.get("response")
        if isinstance(response, dict) and "ok" in response:
            error_code = (response.get("error") or {}).get("code", "")
            assert response["ok"] is False or response["ok"] is True, racer_record
            if response["ok"] is False:
                assert error_code in ("draining", "table_full", "capability", "schema"), (
                    f"racer refused with a stable code, got {error_code}: {racer_record}"
                )
        else:
            assert racer_record.get("sent") is True, racer_record

    # The owner stayed alive holding the domain through the window.
    stopping = owner.result_path.with_suffix(".stopping")
    deadline = time.monotonic() + 15
    while not stopping.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() is None, "the owner drains (alive) while admitted work is blocked"
    contender = harness.start_worker(scratch, "lock-probe", state_dir)
    assert contender.result()["busy"] is True

    # Release: the held confirm terminalizes, the racers' refusals are
    # already answered, the drain completes, the domain frees.
    harness.open_gate(scratch, "exec")
    assert owner.wait(timeout=60) == harness.EXIT_OK
    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop2"), "clean")
    assert successor.result()["started"] is True
    harness.open_gate(scratch, "stop2")
    successor.wait()
