"""M7 Layer 7 round two, tranche two — T65: supported-fault live
ownership-loss qualification.

Frozen law (M7 §9.4 / RV03 / RV17): there is NO supported transition in
which the OS owner lock disappears while the owner process remains
alive and mutation-capable. Ownership ends only by the owner's
deliberate private-handle close after quiescence, or by OS cleanup
after process death. Each test injects one SUPPORTED platform fault on
a live, serving, full-production owner and observes, at process/OS
boundaries, that (a) a real successor process is still refused the
domain, and (b) the owner remains mutation-capable (it still executes
admitted mutation work through the real transport).

Deliberately NOT injected: adversarial destruction of the authority
lock file itself (unlink/replace of authority.lock). That is outside
the supported platform fault contract — the same explicitly-outside-
claim class as M7-T56 — and is not a "spontaneous" platform fault.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path

import pytest

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


def _serving_owner(
    scratch: Path,
    state_dir: Path,
    tag: str,
    fault_marker: Path = None,  # type: ignore[assignment]
    churn_marker: Path = None,  # type: ignore[assignment]
):
    events_path = scratch / f"events-{tag}.ndjson"
    args = [
        str(state_dir),
        str(harness.gate(scratch, f"stop-{tag}")),
        str(events_path),
        str(fault_marker) if fault_marker is not None else "",
        str(churn_marker) if churn_marker is not None else "",
    ]
    owner = harness.start_worker(scratch, "full-owner-serving", *args)
    record = owner.result()
    assert record["started"] is True, record
    return owner, record, events_path


def _invoke_count(events_path: Path) -> int:
    if not events_path.exists():
        return 0
    return len([line for line in events_path.read_text(encoding="utf-8").splitlines() if '"invoke"' in line])


def _preview(scratch: Path, record: dict, text: str) -> str:
    """One REAL preview through the production transport: mints a real
    confirmation token — mutation authority — from a separate client
    process."""
    env = _payload_file(scratch, {"text": text})
    client = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_text",
        env,
        secrets.token_hex(16),
        "normal",
    )
    response = client.result()["response"]
    assert isinstance(response, dict) and response.get("ok"), f"preview failed: {response}"
    return response["data"]["data"]["confirmation_token"]


def _confirm(scratch: Path, record: dict, text: str, token: str) -> dict:
    env = _payload_file(scratch, {"text": text, "confirmation_token": token})
    client = harness.start_worker(
        scratch,
        "ipc-request",
        Path(record["endpoint"]),
        record["build_id"],
        record["instance_id"],
        "post_text",
        env,
        secrets.token_hex(16),
        "normal",
    )
    return client.result()["response"]


def _assert_successor_refused(scratch: Path, state_dir: Path, why: str) -> None:
    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    probe = successor.result()
    assert probe["acquired"] is False and probe["busy"] is True, (
        f"{why}: a real successor must still be refused while the owner lives"
    )
    assert successor.wait() == harness.EXIT_BUSY


def test_T65_owner_local_descriptor_churn_never_releases_live_ownership(tmp_path: Path) -> None:
    """T65 fault class 1 PROPER (F-113): RV03's hazard executes INSIDE
    the lock-owning process — while the owner holds its private owner
    handle and runs a production Dispatcher, the OWNER ITSELF opens and
    closes a SECOND descriptor to authority.lock 40 times. A
    process-associated record-lock primitive (POSIX fcntl record locks)
    RELEASES on any same-process descriptor close; the qualified
    open-file-description primitive must not. Then a real successor is
    refused, and the owner completes a REAL admitted mutation (preview
    + confirm through the durable commit gate to verified effect)
    afterwards — alive and mutation-capable through the whole fault."""
    scratch = _scratch(tmp_path, "t65ownchurn")
    state_dir = scratch / "state"
    churn_marker = scratch / "owner-local-churn.marker"

    owner, record, events_path = _serving_owner(
        scratch, state_dir, "ownchurn", churn_marker=churn_marker
    )
    try:
        churn = harness.wait_record(churn_marker)
        assert churn == {"owner_local_churn_done": True}

        _assert_successor_refused(
            scratch, state_dir, "owner-local churn must not release live ownership"
        )

        # The owner remains mutation-capable past the fault: a full real
        # mutation (token mint through durable confirmed effect).
        token = _preview(scratch, record, "t65 owner-local churn mutation")
        response = _confirm(scratch, record, "t65 owner-local churn mutation", token)
        assert isinstance(response, dict) and response.get("ok"), (
            f"the owner completes a real admitted mutation after owner-local churn: {response}"
        )
        assert _invoke_count(events_path) == 2, "exactly the preview + confirm pair"
    finally:
        harness.open_gate(scratch, "stop-ownchurn")
        owner.wait()

    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True
    assert successor.wait() == harness.EXIT_OK


def test_T65_foreign_descriptor_churn_never_releases_live_ownership(tmp_path: Path) -> None:
    """T65 fault class 1, SUPPLEMENTARY foreign-process half: a
    DIFFERENT process (this controller) churns the authority lock file —
    repeated open/read/close and O_RDWR open/close cycles — against a
    live, serving, full-production owner. This is cross-process noise
    the owner must also tolerate, but it is NOT RV03's owner-local
    hazard (that is the test above); a disallowed process-associated
    record-lock implementation could pass this half, which is why it is
    supplementary and not the RV03 experiment. The owner's OS ownership
    must survive every cycle: a real successor is still refused, and
    the owner still mints confirmation tokens (mutation-capable)
    through the real transport afterwards."""
    scratch = _scratch(tmp_path, "t65churn")
    state_dir = scratch / "state"
    lock_path = state_dir / "authority.lock"

    owner, record, events_path = _serving_owner(scratch, state_dir, "churn")
    try:
        token_before = _preview(scratch, record, "t65 churn baseline")
        assert token_before
        invokes_before = _invoke_count(events_path)

        # The fault: 40 unrelated foreign descriptor cycles against the
        # live owner's lock file — read-only and read-write handles,
        # opened and closed by a different process. The CLOSE is the
        # RV03 threat; reads are incidental (on Windows the CRT region
        # lock is mandatory, so a foreign read INSIDE the locked byte is
        # OS-refused — itself an observation of the primitive, not a
        # failure of the churn).
        reads_blocked = 0
        for _cycle in range(40):
            fd = os.open(lock_path, os.O_RDONLY)
            try:
                os.read(fd, 16)
            except PermissionError:
                reads_blocked += 1
            finally:
                os.close(fd)
            fd = os.open(lock_path, os.O_RDWR)
            os.close(fd)
        assert reads_blocked == 0 or os.name == "nt", (
            "POSIX flock is advisory: foreign reads must not be OS-refused there"
        )

        _assert_successor_refused(
            scratch, state_dir, "foreign descriptor churn must not release live ownership"
        )

        token_after = _preview(scratch, record, "t65 churn after-fault")
        assert token_after and token_after != token_before, (
            "the owner remains mutation-capable after the fault: a fresh "
            "confirmation token is minted through the real transport"
        )
        assert _invoke_count(events_path) == invokes_before + 1
    finally:
        harness.open_gate(scratch, "stop-churn")
        owner.wait()

    # After the CONTROLLED release, a successor acquires: the refusal
    # above was live-ownership, not a broken domain.
    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True
    assert successor.wait() == harness.EXIT_OK


@pytest.mark.skipif(os.name != "posix", reason="rendezvous unlink is a POSIX-pathname fault")
def test_T65_endpoint_rendezvous_loss_is_not_ownership_loss(tmp_path: Path) -> None:
    """T65 fault class 2 — the T49 fault family striking a LIVE owner:
    the endpoint rendezvous pathname is deleted while the owner lives
    (on POSIX, the domain socket file). The rendezvous was never
    ownership: a real successor is still refused; a connection that was
    ESTABLISHED before the loss still delivers and is served (the owner
    remains mutation-capable); and only after the owner's real death
    does a successor acquire and rebind the endpoint."""
    scratch = _scratch(tmp_path, "t65path")
    state_dir = scratch / "state"

    owner, record, events_path = _serving_owner(scratch, state_dir, "path")
    endpoint = Path(record["endpoint"])
    send_gate = harness.gate(scratch, "send")
    held = None
    try:
        # A client CONNECTS now, then holds its request until after the
        # rendezvous loss (the T16 send-gate pattern, inverted use).
        env = _payload_file(scratch, {"text": "t65 established-connection preview"})
        held = harness.start_worker(
            scratch,
            "ipc-request",
            endpoint,
            record["build_id"],
            record["instance_id"],
            "post_text",
            env,
            secrets.token_hex(16),
            "normal",
            send_gate,
        )
        assert harness.wait_record(held.result_path.with_suffix(".connected")) == {"connected": True}

        # The fault: the live owner's rendezvous pathname disappears.
        endpoint.unlink()
        assert not endpoint.exists()

        _assert_successor_refused(
            scratch, state_dir, "rendezvous loss must not become ownership loss"
        )

        # The ESTABLISHED connection still delivers and is served: the
        # owner executes admitted mutation work past the fault.
        harness.open_gate(scratch, "send")
        response = held.result()["response"]
        assert isinstance(response, dict) and response.get("ok"), (
            f"the established connection is served after rendezvous loss: {response}"
        )
        assert response["data"]["data"]["confirmation_token"]
        assert _invoke_count(events_path) == 1
    except BaseException:
        # Cleanup on failure only — on success the owner must NOT stop
        # cleanly here: the takeover phase below needs a real unclean death.
        harness.open_gate(scratch, "send")
        if held is not None:
            held.wait(timeout=30)
        owner.kill()
        owner.wait(timeout=30)
        raise

    # Real unclean death (controller kill — the OS is the only cleanup):
    # only now may a successor acquire, and it REBINDS the endpoint.
    owner.kill()
    owner.wait(timeout=30)
    assert owner.returncode is not None, "the OS must terminate the owner"

    successor = harness.start_worker(
        scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean"
    )
    successor_record = successor.result()
    assert successor_record["started"] is True, successor_record
    try:
        assert Path(successor_record["endpoint"]) == endpoint
        assert endpoint.exists(), "the successor rebound the endpoint pathname under ownership"
    finally:
        harness.open_gate(scratch, "stop-path")
        harness.open_gate(scratch, "done")
        successor.wait()
        owner.wait(timeout=30)


def test_T65_durable_append_io_fault_degrades_the_request_not_the_ownership(tmp_path: Path) -> None:
    """T65 fault class 3 — a supported I/O fault INSIDE the owner's own
    durable append path: the effect-ledger append raises one injected
    ENOSPC OSError at the next mutation's durability seam. The law: the
    REQUEST degrades (an error outcome over the real transport — the
    owner never reports success for work that is not durable), the
    OWNER survives (a real successor is still refused), and the owner
    remains mutation-capable — the very next mutation completes
    end-to-end through the real executor stack once the transient
    fault has passed."""
    scratch = _scratch(tmp_path, "t65io")
    state_dir = scratch / "state"
    fault_marker = scratch / "fault-once.marker"

    owner, record, events_path = _serving_owner(scratch, state_dir, "io", fault_marker)
    try:
        # Mutation 1 crosses the faulted append: the durable reservation
        # seam raises ENOSPC. The response must be an ERROR outcome.
        token1 = _preview(scratch, record, "t65 io faulted mutation")
        response1 = _confirm(scratch, record, "t65 io faulted mutation", token1)
        assert isinstance(response1, dict) and not response1.get("ok", True), (
            f"a faulted durability append must degrade the request, never report success: {response1}"
        )
        assert fault_marker.exists(), "the injected fault actually fired"
        _assert_successor_refused(
            scratch, state_dir, "an in-owner I/O fault must not release live ownership"
        )

        # Mutation 2 — the transient fault is spent; the owner completes
        # the full real mutation (durable reservation through verified
        # effect) through the same live process.
        token2 = _preview(scratch, record, "t65 io recovered mutation")
        response2 = _confirm(scratch, record, "t65 io recovered mutation", token2)
        assert isinstance(response2, dict) and response2.get("ok"), (
            f"the owner remains mutation-capable after the I/O fault: {response2}"
        )
    finally:
        harness.open_gate(scratch, "stop-io")
        owner.wait()
