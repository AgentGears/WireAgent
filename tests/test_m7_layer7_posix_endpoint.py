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


# The foreign-uid probe: a standalone instrument (no repo imports) that
# connects a plain AF_UNIX socket to the live endpoint and reports, as
# stdout JSON, whether the owner served the connection or closed it
# before any hello byte. The production transport speaks first (hello
# on accept), so an accepted peer reads a byte immediately and a
# rejected peer reads clean EOF. Its own uid ships in the record so the
# controller proves the identity boundary actually differed.
_PEER_PROBE_SOURCE = (
    "import json, os, socket, sys\n"
    "record = {'uid': os.getuid(), 'pid': os.getpid()}\n"
    "try:\n"
    "    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)\n"
    "    s.settimeout(10)\n"
    "    s.connect(sys.argv[1])\n"
    "    record['connected'] = True\n"
    "    try:\n"
    "        data = s.recv(1)\n"
    "        record['first_byte'] = data.hex() if data else ''\n"
    "    except socket.timeout:\n"
    "        record['first_byte'] = 'timeout'\n"
    "    except ConnectionError as exc:\n"
    "        record['first_byte'] = 'reset:' + type(exc).__name__\n"
    "    s.close()\n"
    "except OSError as exc:\n"
    "    record['connected'] = False\n"
    "    record['connect_error'] = repr(exc)\n"
    "print(json.dumps(record, sort_keys=True), flush=True)\n"
)


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
    """T63's fork core (F-123: the same survivor-liveness discipline the
    spawn/exec variant received in F-117): a parent acquires ownership,
    FORKS; the parent dies uncleanly with the child alive (the child
    inherited the raw descriptor). The fork child self-reports its pid
    AND kernel start time, and the SAME child — alive, non-zombie,
    identity-consistent — is verified immediately BEFORE the successor
    acquires and again AFTER: a successor MUST acquire the domain while
    the fork child genuinely survives, not merely after a child once
    existed. The inherited descriptor cannot preserve or extend the
    dead parent's ownership."""
    scratch = _scratch(tmp_path, "fork")
    state_dir = scratch / "state"
    parent = successor = None
    child_pid = None
    child_starttime = None

    try:
        parent = harness.start_worker(scratch, "fork-child", state_dir, harness.gate(scratch, "release"))
        record = parent.result()
        assert record["acquired"] is True, record
        child_json = parent.result_path.with_suffix(".child.json")
        child_record = harness.wait_record(child_json)
        assert child_record["child_alive"] is True
        child_pid = child_record["pid"]
        child_starttime = child_record.get("starttime")
        assert child_pid == record["child_pid"], "the fork child is the parent's forked child"

        # The fork child is alive and identity-tied BEFORE the parent dies.
        assert harness.pid_alive(child_pid), "the fork child is executing before the parent dies"
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime

        # Parent dies uncleanly: fork-child's parent os._exit(9)s
        # immediately after writing its record; the release gate is the
        # CHILD's exit gate. Wait for the parent's unclean death now.
        deadline = time.monotonic() + 15
        while parent.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert parent.poll() == harness.EXIT_DIED_UNCLEAN, "the parent died uncleanly"

        # F-123 checkpoint A: the fork child SURVIVED the parent's
        # death — alive, non-zombie, identity-tied — immediately BEFORE
        # the successor acquisition.
        assert harness.pid_alive(child_pid), (
            "the fork child must still be alive when the successor acquires"
        )
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime

        # THE LAW: with the forked child still alive, a successor acquires.
        successor = harness.start_worker(scratch, "lock-probe", state_dir)
        assert successor.result()["acquired"] is True, (
            "an inherited descriptor must not keep the domain locked after parent death"
        )
        assert successor.wait() == harness.EXIT_OK

        # F-123 checkpoint B: the SAME fork child is still alive AFTER
        # the successor acquired — takeover happened under genuine
        # child survival.
        assert harness.pid_alive(child_pid), (
            "the same fork child is still alive after the successor acquired"
        )
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime
    finally:
        # F-126: independent best-effort cleanup per gate/process, with
        # identity-aware last-resort kills.
        harness.safe_open_gate(scratch, "release")
        if parent is not None and parent.poll() is None:
            parent.kill()
        if successor is not None and successor.poll() is None:
            successor.kill()
        if child_pid is not None:
            deadline = time.monotonic() + 5
            while harness.pid_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            harness.kill_pid_if_same_process(child_pid, child_starttime)


def test_T63_fork_negative_control_dead_child_is_detected(tmp_path: Path) -> None:
    """F-123's adversarial acceptance test (fork variant): deliberately
    terminate the fork child AFTER its readiness record but BEFORE the
    parent's death is observed by the controller. The liveness oracle
    MUST report the child dead — proving the fork-survivor assertions
    above cannot pass vacuously with a child that died early."""
    scratch = _scratch(tmp_path, "forkctl")
    state_dir = scratch / "state"
    parent = None
    child_pid = None
    child_starttime = None

    try:
        parent = harness.start_worker(scratch, "fork-child", state_dir, harness.gate(scratch, "release"))
        record = parent.result()
        assert record["acquired"] is True, record
        child_json = parent.result_path.with_suffix(".child.json")
        child_record = harness.wait_record(child_json)
        child_pid = child_record["pid"]
        child_starttime = child_record.get("starttime")
        assert harness.pid_alive(child_pid)

        # The adversarial act: kill the fork child while the parent has
        # written its record (the parent exit(9)s immediately after, so
        # it is dead or dying — the takeover precondition is what the
        # oracle must now refuse).
        os.kill(child_pid, 9)
        deadline = time.monotonic() + 10
        while harness.pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        # THE NEGATIVE CONTROL: the oracle reports the fork child dead.
        assert harness.pid_alive(child_pid) is False, (
            "the liveness oracle must detect a terminated fork child"
        )
        if child_starttime is not None:
            # The identity tie agrees: no same-starttime process remains.
            current = harness.proc_starttime(child_pid)
            assert current is None or current != child_starttime
    finally:
        harness.safe_open_gate(scratch, "release")
        if parent is not None and parent.poll() is None:
            parent.kill()


def test_spawn_exec_child_cannot_inherit_use_or_keep_ownership(tmp_path: Path) -> None:
    """T63's spawn/exec half (the variant the round-one fork test left
    open): the parent acquires, spawns a REAL exec'd child — a fresh
    interpreter image through the qualified subprocess path, launched
    with close_fds=False so the production close-on-exec contract is
    the ONLY thing standing between the child and the owner descriptor.
    Four laws, all observed at process/OS boundaries: (1) the child's
    OWN fd table contains no descriptor resolving to authority.lock
    (self-inspected through /proc, where the platform exposes it);
    (2) the child cannot USE the living parent's ownership — its own
    AuthorityOwnerLock.acquire() is refused authority_busy while the
    parent holds; (3) the exec'd child is STILL ALIVE — zombie-
    rejecting, identity-tied to its self-reported /proc starttime —
    both immediately before and immediately after the successor's
    acquisition (F-117: takeover must occur WHILE the child survives,
    not merely after a child existed); (4) after the parent's unclean
    death a successor acquires — no retained, extended, released, or
    exercised ownership survives exec."""
    scratch = _scratch(tmp_path, "spawn")
    state_dir = scratch / "state"
    parent = successor = None
    child_pid = None

    try:
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
        child_pid = child_record["pid"]
        assert child_pid == record["child_pid"], "the exec'd child is the parent's spawned child"
        child_starttime = child_record.get("starttime")

        # Law 2 — the contender observation is only meaningful while the
        # parent provably still lives (it dies on its gate, still closed).
        assert parent.poll() is None, "the parent must still be alive at the child's acquire attempt"
        assert child_record["child_acquire"] == "busy", (
            "a surviving exec'd child cannot use the living parent's ownership"
        )
        # The surviving child is ALIVE and is THE SAME process (identity
        # tie) BEFORE the parent dies.
        assert harness.pid_alive(child_pid), "the exec'd child is executing before the parent dies"
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime, (
                "the surviving child is the same process (starttime identity tie)"
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

        # F-117 checkpoint A: the child SURVIVES the parent's death —
        # alive, non-zombie, identity-tied — immediately BEFORE the
        # successor acquisition.
        assert harness.pid_alive(child_pid), (
            "the exec'd child must still be alive when the successor acquires"
        )
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime

        # Law 4 — successor acquisition follows the chosen primitive
        # contract: while the exec'd child still lives, a successor acquires.
        successor = harness.start_worker(scratch, "lock-probe", state_dir)
        assert successor.result()["acquired"] is True, (
            "an exec'd child must not keep the domain locked after parent death"
        )
        assert successor.wait() == harness.EXIT_OK

        # F-117 checkpoint B: the SAME child is STILL alive after the
        # successor acquired and exited — the takeover happened while
        # the child genuinely survived.
        assert harness.pid_alive(child_pid), (
            "the same exec'd child is still alive after the successor acquired"
        )
        if child_starttime is not None:
            assert harness.proc_starttime(child_pid) == child_starttime, (
                "the surviving pid is the SAME process, not a reused pid"
            )
    finally:
        # Exception-safe cleanup (F-120/F-126): each gate and process is
        # handled independently — one failure must not strand the others
        # — and the last-resort pid kill is identity-aware (a reused pid
        # is never killed).
        harness.safe_open_gate(scratch, "die")
        harness.safe_open_gate(scratch, "child")
        if parent is not None and parent.poll() is None:
            parent.kill()
        if successor is not None and successor.poll() is None:
            successor.kill()
        if child_pid is not None:
            deadline = time.monotonic() + 5
            while harness.pid_alive(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            harness.kill_pid_if_same_process(child_pid, child_starttime)


def test_T63_negative_control_dead_child_is_detected(tmp_path: Path) -> None:
    """F-117's adversarial acceptance test: deliberately terminate the
    exec'd child AFTER it writes its readiness record but BEFORE the
    parent dies. The liveness oracle MUST detect the dead child —
    proving the survivor assertions in the T63 test above cannot pass
    vacuously with a child that died early."""
    scratch = _scratch(tmp_path, "spawnctl")
    state_dir = scratch / "state"
    parent = None
    child_pid = None

    try:
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
        child_pid = child_record["pid"]
        assert harness.pid_alive(child_pid)

        # The adversarial act: kill the child while the parent still
        # holds the domain.
        os.kill(child_pid, 9)
        deadline = time.monotonic() + 10
        while harness.pid_alive(child_pid) and time.monotonic() < deadline:
            time.sleep(0.05)

        # THE NEGATIVE CONTROL: the oracle reports the child dead. If
        # this fails, the survivor assertion in the real T63 test is
        # vacuous (a zombie or stale pid would satisfy it).
        assert harness.pid_alive(child_pid) is False, (
            "the liveness oracle must detect a terminated child"
        )
    finally:
        harness.safe_open_gate(scratch, "die")
        harness.safe_open_gate(scratch, "child")
        if parent is not None and parent.poll() is None:
            parent.kill()


def _foreign_uid_launcher():
    """The argv prefix that launches a genuinely foreign-uid process in
    this environment, or None when no identity difference can be
    obtained (the T60 rejection half then skips rather than probing
    with a same-uid stand-in). Non-root environments use passwordless
    sudo + nobody; root environments (containers, some runners) use
    setpriv to drop to the classic unprivileged uid directly. The
    preflight (F-116) runs the ACTUAL interpreter under the foreign
    identity — executing ``id`` proves nothing about whether that uid
    can load this Python and its dependencies, and a root-private
    installation would otherwise pass the preflight and fail the
    probe."""
    import subprocess

    if os.name != "posix":
        return None
    candidates = (
        (["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"], os.geteuid() == 0),
        (["sudo", "-n", "-u", "nobody"], os.geteuid() != 0),
    )
    for prefix, applicable in candidates:
        if not applicable:
            continue
        try:
            probe = subprocess.run(
                [
                    *prefix,  # noqa: S603, S607 - qualification preflight
                    sys.executable,
                    "-c",
                    "import json, socket; print('interpreter-ok')",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0 and "interpreter-ok" in probe.stdout:
            return prefix
    return None


def test_foreign_uid_peer_rejected_before_hello_listener_survives(tmp_path: Path) -> None:
    """T60's peer-identity half (the part the round-one socket-mode test
    left open): a genuinely FOREIGN-UID process — a real identity
    difference at the kernel boundary, not a same-uid stand-in —
    connects to the live production endpoint. The 0600 socket mode and
    the 0700 tree are the FIRST boundary, so the controller
    deliberately widens traversal and the rendezvous mode to 0666 to
    let the foreign process REACH the endpoint and probe the SECOND
    boundary: production SO_PEERCRED checking. The foreign peer is
    closed BEFORE any hello byte; the listener keeps serving same-user
    peers afterwards (rejection is not a listener failure). F-119: the
    ENTIRE experiment runs in an independently owned, disposable tree
    directly under /tmp — pytest's shared tmp_path hierarchy is never
    touched or widened. Skips where no foreign uid can be obtained."""
    import subprocess

    if not hasattr(socket, "SO_PEERCRED"):
        pytest.skip("SO_PEERCRED (the production peer-identity check) is not exposed on this platform")
    launcher = _foreign_uid_launcher()
    if launcher is None:
        pytest.skip("no foreign-uid launcher here; the foreign-uid rejection half cannot be probed")

    import tempfile

    root = Path(tempfile.mkdtemp(prefix="webwire-t60-", dir="/tmp"))
    scratch = root / "peerid"
    scratch.mkdir()
    state_dir = scratch / "state"
    owner = None

    try:
        owner = harness.start_worker(
            scratch, "full-owner", state_dir, harness.gate(scratch, "stop"), "clean"
        )
        record = owner.result()
        assert record["started"] is True
        endpoint = Path(record["endpoint"])

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

        # Stage a SELF-CONTAINED probe script in the SAME isolated tree:
        # the probe is a test INSTRUMENT — the boundary under test is
        # the server's SO_PEERCRED check — so it needs no repo access.
        # (Runner home directories are typically 750: a foreign uid
        # cannot traverse into the checkout at all, and widening the
        # runner's home is not ours to do.)
        staging = root / "probe"
        staging.mkdir()
        probe_script = staging / "peer_probe.py"
        probe_script.write_text(_PEER_PROBE_SOURCE, encoding="utf-8")

        # Deliberately widen ONLY this disposable tree — the rendezvous
        # mode to 0666, traversal on THIS RUN'S OWN directories — so the
        # foreign-uid process can reach the endpoint at all; the
        # production peer check underneath is the boundary under
        # qualification. /tmp itself is 1777; the tree is destroyed in
        # the finally block, so no widening survives the test.
        endpoint.chmod(0o666)
        for directory in (staging, scratch, root):
            directory.chmod(0o755)
        state_dir.chmod(state_dir.stat().st_mode | 0o111)

        foreign = subprocess.run(
            [
                *launcher,  # noqa: S603 - the qualification probe itself
                sys.executable,
                str(probe_script),
                str(endpoint),
            ],
            capture_output=True,
            text=True,
            timeout=30,
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
        if owner is not None:
            harness.open_gate(scratch, "stop")
            try:
                owner.wait(timeout=30)
            except Exception:  # noqa: BLE001 - cleanup must never mask the failure
                owner.kill()
        import shutil

        shutil.rmtree(root, ignore_errors=True)


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
