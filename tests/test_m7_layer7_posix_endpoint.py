"""M7 Layer 7 — POSIX-only endpoint security and child survival (lane 5
+ T63). These scenarios require AF_UNIX, O_NOFOLLOW, and os.fork: they
qualify on the Linux CI runners and skip elsewhere.
"""

from __future__ import annotations

import json
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


def test_spawn_exec_child_cannot_inherit_use_or_keep_ownership(tmp_path: Path) -> None:
    """T63's spawn/exec half (the variant the round-one fork test left
    open): the parent acquires, spawns a REAL exec'd child — a fresh
    interpreter image through the qualified subprocess path, launched
    with close_fds=False so the production close-on-exec contract is
    the ONLY thing standing between the child and the owner descriptor.
    Three laws, all observed at process/OS boundaries: (1) the child's
    OWN fd table contains no descriptor resolving to authority.lock
    (self-inspected through /proc, where the platform exposes it);
    (2) the child cannot USE the living parent's ownership — its own
    AuthorityOwnerLock.acquire() is refused authority_busy while the
    parent holds; (3) after the parent's unclean death a successor
    acquires while the exec'd child still lives — no retained, extended,
    released, or exercised ownership survives exec."""
    scratch = _scratch(tmp_path, "spawn")
    state_dir = scratch / "state"

    parent = harness.start_worker(
        scratch,
        "owner-spawn-child",
        state_dir,
        harness.gate(scratch, "die"),
        harness.gate(scratch, "child"),
    )
    record = parent.result()
    assert record["acquired"] is True, record
    child_json = parent.result_path.with_suffix(".child.json")
    child_record = harness.wait_record(child_json)
    assert child_record["child_alive"] is True
    assert child_record["pid"] == record["child_pid"], "the exec'd child is the parent's spawned child"

    # Law 2 — the contender observation is only meaningful while the
    # parent provably still lives (it dies on its gate, still closed).
    assert parent.poll() is None, "the parent must still be alive at the child's acquire attempt"
    assert child_record["child_acquire"] == "busy", (
        "a surviving exec'd child cannot use the living parent's ownership"
    )

    # Law 1 — the empirical close-on-exec observation (where /proc
    # exposes the child's own fd table; Linux CI carries it).
    if "inherited_lock_fd" in child_record:
        assert child_record["inherited_lock_fd"] is False, (
            "the owner descriptor must not survive exec into the child's fd table"
        )

    harness.open_gate(scratch, "die")
    deadline = time.monotonic() + 15
    while parent.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert parent.poll() == harness.EXIT_DIED_UNCLEAN, "the parent died uncleanly with the child alive"

    # Law 3 — successor acquisition follows the chosen primitive
    # contract: while the exec'd child still lives, a successor acquires.
    successor = harness.start_worker(scratch, "lock-probe", state_dir)
    assert successor.result()["acquired"] is True, (
        "an exec'd child must not keep the domain locked after parent death"
    )
    assert successor.wait() == harness.EXIT_OK

    harness.open_gate(scratch, "child")  # let the exec'd child exit


def _foreign_uid_launcher():
    """The argv prefix that launches a genuinely foreign-uid process in
    this environment, or None when no identity difference can be
    obtained (the T60 rejection half then skips rather than probing
    with a same-uid stand-in). Non-root environments use passwordless
    sudo + nobody; root environments (containers, some runners) use
    setpriv to drop to the classic unprivileged uid directly."""
    import subprocess

    if os.name != "posix":
        return None
    if os.geteuid() == 0:
        try:
            probe = subprocess.run(
                [
                    "setpriv",  # noqa: S603, S607 - qualification probe
                    "--reuid=65534",
                    "--regid=65534",
                    "--clear-groups",
                    "id",
                    "-u",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if probe.returncode == 0 and probe.stdout.strip() == "65534":
            return ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"]
        return None
    try:
        probe = subprocess.run(
            ["sudo", "-n", "-u", "nobody", "id", "-u"],  # noqa: S603, S607 - qualification probe
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if probe.returncode == 0 and probe.stdout.strip().isdigit():
        return ["sudo", "-n", "-u", "nobody"]
    return None


def test_foreign_uid_peer_rejected_before_hello_listener_survives(tmp_path: Path) -> None:
    """T60's peer-identity half (the part the round-one socket-mode test
    left open): a genuinely FOREIGN-UID process — a real identity
    difference at the kernel boundary, not a same-uid stand-in —
    connects to the live production endpoint. The 0600 socket mode and
    the 0700 scratch tree are the FIRST boundary, so the controller
    deliberately widens traversal and the rendezvous mode to 0666 to
    let the foreign process REACH the endpoint and probe the SECOND
    boundary: production SO_PEERCRED checking. The foreign peer is
    closed BEFORE any hello byte; the listener keeps serving same-user
    peers afterwards (rejection is not a listener failure). Skips
    where no foreign uid can be obtained."""
    import subprocess

    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("SO_PEERCRED (the production peer-identity check) is not exposed on this platform")
    launcher = _foreign_uid_launcher()
    if launcher is None:
        pytest.skip("no foreign-uid launcher here; the foreign-uid rejection half cannot be probed")

    scratch = _scratch(tmp_path, "peerid")
    state_dir = scratch / "state"

    owner = harness.start_worker(scratch, "full-owner", state_dir, harness.gate(scratch, "stop"), "clean")
    record = owner.result()
    assert record["started"] is True
    endpoint = Path(record["endpoint"])

    try:
        # Baseline: a same-user peer from a DIFFERENT process is served
        # (the peer check must not over-restrict valid local clients).
        # T21 precedent: with the stub DOM surface, health returns a
        # well-formed ok:false diagnostic — "served" means the exchange
        # completed over the transport, which itself proves the hello
        # frame reached an accepted same-user peer.
        def _same_user_served(tag: str) -> None:
            import secrets as _secrets

            env = scratch / f"health-{tag}.json"
            env.write_text(json.dumps({}), encoding="utf-8")
            client = harness.start_worker(
                scratch,
                "ipc-request",
                endpoint,
                record["build_id"],
                record["instance_id"],
                "health",
                env,
                _secrets.token_hex(16),
                "normal",
            )
            response = client.result()["response"]
            assert isinstance(response, dict) and "ok" in response, f"same-user peer served: {response}"

        _same_user_served("baseline")

        # Deliberately widen ONLY the traversal + rendezvous mode so the
        # foreign-uid process can reach the endpoint at all; the
        # production peer check underneath is the boundary under
        # qualification.
        endpoint.chmod(0o666)
        for directory in (endpoint.parent, scratch, tmp_path, *tmp_path.parents[:2]):
            directory.chmod(directory.stat().st_mode | 0o111)

        foreign = subprocess.run(
            [
                *launcher,  # noqa: S603 - the qualification probe itself
                sys.executable,
                str(harness.WORKER),
                "peer-probe",
                str(scratch / "unused-result.json"),
                str(endpoint),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        assert foreign.returncode == 0, f"peer probe failed: {foreign.stderr[-400:]!r}"
        probe_record = json.loads(foreign.stdout.strip().splitlines()[-1])
        assert probe_record["connected"] is True, probe_record
        assert probe_record["uid"] != os.getuid(), "the probe must genuinely run under a foreign uid"
        assert probe_record["first_byte"] == "", (
            f"the foreign-uid peer is closed BEFORE any hello byte: {probe_record}"
        )

        # Rejection is not a listener failure: a same-user peer from a
        # different process is still served after the rejection.
        _same_user_served("after-rejection")
    finally:
        harness.open_gate(scratch, "stop")
        owner.wait()


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
