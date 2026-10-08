"""M7 Layer 7 — POSIX-only endpoint security and child survival (lane 5
+ T63). These scenarios require AF_UNIX, O_NOFOLLOW, and os.fork: they
qualify on the Linux CI runners and skip elsewhere.
"""

from __future__ import annotations

import os
import socket
import stat
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import _m7_layer7_harness as harness  # noqa: E402

pytestmark = pytest.mark.skipif(
    not hasattr(socket, "AF_UNIX") or not hasattr(os, "fork"),
    reason="POSIX-only: AF_UNIX + fork are the mechanisms under qualification",
)


def _scratch(tmp_path: Path, name: str) -> Path:
    scratch = tmp_path / name
    scratch.mkdir(parents=True, exist_ok=True)
    return scratch


def test_socket_permissions_are_owner_restricted(tmp_path: Path) -> None:
    """T60 EMPIRICAL: the live domain socket's mode is 0600 (owner
    read/write only) and its parent directory is the canonical authority
    domain — the qualified owner-restricted same-user boundary,
    observed on the actual platform rather than asserted
    structurally."""
    scratch = _scratch(tmp_path, "perms")
    state_dir = scratch / "state"

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop"), "clean")
    record = owner.result()
    assert record["started"] is True
    try:
        endpoint = Path(record["endpoint"])
        assert endpoint.exists(), "the domain socket rendezvous exists while the owner lives"
        mode = stat.S_IMODE(endpoint.stat().st_mode)
        assert mode == 0o600, f"socket mode {oct(mode)} is not owner-restricted 0600"
        assert endpoint.parent == state_dir, "the rendezvous lives in the authority domain"
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


def test_unclean_death_leaves_stale_path_successor_rebinds_only_when_owning(tmp_path: Path) -> None:
    """T49 EMPIRICAL: after an unclean owner death the socket PATHNAME
    remains on disk (stale). The successor — a real process — acquires
    the domain, REMOVES the stale rendezvous, and binds its own; the
    stale file never blocks acquisition, and removal happens only under
    ownership."""
    scratch = _scratch(tmp_path, "stalepath")
    state_dir = scratch / "state"

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "die"), "die")
    record = owner.result()
    endpoint = Path(record["endpoint"])
    assert endpoint.exists()

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while owner.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert owner.poll() == harness.EXIT_DIED_UNCLEAN
    assert endpoint.exists(), "an unclean death leaves the stale pathname behind"

    successor = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "done"), "clean")
    successor_record = successor.result()
    assert successor_record["started"] is True
    try:
        # The successor removed the stale inode and bound its own at the
        # same path (same rendezvous identity, new socket object).
        assert endpoint.exists()
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5)
        client.connect(str(endpoint))
        client.close()
    finally:
        harness.open_gate(scratch, "done")
        successor.wait()


def test_fork_child_cannot_keep_domain_locked_after_parent_death(tmp_path: Path) -> None:
    """T63's core: a parent acquires ownership, FORKS; the parent dies
    uncleanly with the child alive (the child inherited the raw
    descriptor). A successor process MUST acquire the domain while the
    child still lives — the child cannot preserve or extend the dead
    parent's ownership through the inherited descriptor."""
    scratch = _scratch(tmp_path, "fork")
    state_dir = scratch / "state"

    parent = harness.start_worker(scratch, "fork-child", state_dir, harness.gate(scratch, "release"))
    record = parent.result()
    assert record["acquired"] is True, record
    child_json = parent.result_path.with_suffix(".child.json")
    child_record = harness.wait_record(child_json)
    assert child_record["child_alive"] is True

    deadline = time.monotonic() + 15
    while parent.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert parent.poll() == harness.EXIT_DIED_UNCLEAN, "the parent died uncleanly"

    # THE LAW: with the forked child still alive, a successor acquires.
    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True, (
        "an inherited descriptor must not keep the domain locked after parent death"
    )
    assert successor.wait() == harness.EXIT_OK

    harness.open_gate(scratch, "release")  # let the child exit


def test_no_tcp_listener_anywhere_in_the_owner(tmp_path: Path) -> None:
    """T51 EMPIRICAL on the qualified platform: the full production
    owner process holds NO listening TCP socket — local transport only.
    Observed through /proc (the OS boundary), not object inspection."""
    scratch = _scratch(tmp_path, "notcp")
    state_dir = scratch / "state"

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop"), "clean")
    record = owner.result()
    assert record["started"] is True
    try:
        pid = owner.process.pid
        # F-97: BOTH the IPv4 and IPv6 tables — an owner with only an
        # IPv6 listener must not pass an IPv4-only check. /proc/net/tcp*
        # is system-wide; filter by sockets OWNED by this process via
        # the fd-inode mapping.
        owned_inodes = set()
        for fd_link in Path(f"/proc/{pid}/fd").iterdir():
            try:
                target = fd_link.readlink()
            except OSError:
                continue
            text = str(target)
            if text.startswith("socket:["):
                owned_inodes.add(text[8:-1])
        listening_owned = []
        for table in (f"/proc/{pid}/net/tcp", f"/proc/{pid}/net/tcp6"):
            for line in Path(table).read_text().splitlines()[1:]:
                fields = line.split()
                if int(fields[3], 16) == 0x0A and fields[9] in owned_inodes:
                    listening_owned.append((table, fields[1]))
        assert listening_owned == [], f"the production owner holds listening TCP sockets: {listening_owned}"
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()
