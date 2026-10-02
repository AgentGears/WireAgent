"""M7 Layer 3 acceptance tests — owner instance identity, stale-session
denial, controlled lifecycle qualification, and crash/forced-death
takeover with no stealing (frozen Layer-3 contract, PR from main b87c673).

Layer-3 boundaries honored: no IPC endpoints, no transport, no persisted
instance IDs, no heartbeat/TTL state. AuthorityOwnerLock stays
encapsulated — the session/lock relationship is proven semantically (a
session exists only after acquisition; deliberate release requires a
TERMINAL session).

Written BEFORE the implementation (test-first per the frozen contract):
every test here fails until authority_instance_id, acquired_at, and
instance-bound admission exist.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from webwire.authority import AuthorityBusyError, AuthorityOwnerLock
from webwire.authority_session import (
    AuthorityAdmissionClosedError,
    AuthoritySession,
    AuthoritySessionError,
    AuthorityStaleInstanceError,
)
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.session import SessionManager

REPO_SRC = str(Path(__file__).resolve().parents[1] / "src")


def _child_env() -> dict:
    env = dict(os.environ)
    parent_path = [os.path.abspath(e) for e in sys.path if e]
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(parent_path + ([inherited] if inherited else []))
    return env


# ---------------------------------------------------------------------------
# 1. Owner instance identity
# ---------------------------------------------------------------------------


def test_instance_id_is_fresh_256bit_per_session() -> None:
    """Every session mints a fresh cryptographically random 256-bit id
    (64 hex chars); no two sessions share one."""
    ids = set()
    for _ in range(64):
        session = AuthoritySession(authority_domain=Path("x"))
        assert session.authority_instance_id is not None
        assert len(session.authority_instance_id) == 64
        int(session.authority_instance_id, 16)  # hex
        ids.add(session.authority_instance_id)
    assert len(ids) == 64, "every session id must be unique"


def test_instance_id_minted_before_ready_and_immutable_with_acquired_at() -> None:
    session = AuthoritySession(authority_domain=Path("x"))
    assert session.state.value == "starting"
    instance_id = session.authority_instance_id
    acquired_at = session.acquired_at
    assert isinstance(acquired_at, float)
    session.activate()
    session.begin_drain()
    session.terminalize()
    # Immutable for that session: stable through the whole lifecycle.
    assert session.authority_instance_id == instance_id
    assert session.acquired_at == acquired_at
    with pytest.raises(AttributeError):
        session.authority_instance_id = "forged"  # type: ignore[misc]


def test_clean_reacquisition_mints_a_different_id_same_domain() -> None:
    """Two successive sessions over the SAME canonical domain (real lock
    acquisition and release between them) mint different ids."""
    domain = Path("same-domain")
    lock_a = AuthorityOwnerLock(domain).acquire()
    session_a = AuthoritySession(authority_domain=domain)
    id_a = session_a.authority_instance_id
    session_a.activate()
    session_a.begin_drain()
    session_a.terminalize()
    lock_a.release()

    lock_b = AuthorityOwnerLock(domain).acquire()
    session_b = AuthoritySession(authority_domain=domain)
    assert session_b.authority_instance_id != id_a
    session_b.activate()
    session_b.begin_drain()
    session_b.terminalize()
    lock_b.release()


# ---------------------------------------------------------------------------
# 2. Stale-session denial before IPC exists (the instance-bound admission
#    primitive Layer 4 will later call)
# ---------------------------------------------------------------------------


def test_stale_instance_id_rejected_before_active_work_increment() -> None:
    """Admission carrying an expected instance id compares it in the SAME
    lifecycle critical section as READY/admission counting: a wrong/old id
    is rejected BEFORE the active-work counter increments."""
    session = AuthoritySession(authority_domain=Path("x"))
    session.activate()
    stale = "f" * 64  # a previous owner's id

    with pytest.raises(AuthorityStaleInstanceError, match="stale"):
        with session.admit(expected_instance_id=stale):
            pass
    assert session.active_work == 0, "stale id must not increment admission"

    # The CURRENT owner's id admits normally.
    with session.admit(expected_instance_id=session.authority_instance_id):
        assert session.active_work == 1
    assert session.active_work == 0

    # The rejection is specific: any other 256-bit hex id is stale too.
    other = "0" * 64
    with pytest.raises(AuthorityStaleInstanceError):
        with session.admit(expected_instance_id=other):
            pass
    assert session.active_work == 0
    session.begin_drain()
    session.terminalize()


def test_stale_id_rejected_even_while_draining_or_terminal() -> None:
    session = AuthoritySession(authority_domain=Path("x"))
    session.activate()
    own = session.authority_instance_id
    session.begin_drain()
    with pytest.raises(AuthorityAdmissionClosedError):
        with session.admit(expected_instance_id=own):
            pass
    session.terminalize()
    with pytest.raises(AuthorityAdmissionClosedError):
        with session.admit(expected_instance_id=own):
            pass


def test_stale_reference_denied_under_a_successor_session() -> None:
    """The reviewer's core sequence at the primitive level: owner A's id is
    captured; A terminalizes and releases; successor B acquires the SAME
    domain and mints a different id; stale-A admission is denied, fresh-B
    admission succeeds — under B's READY state."""
    domain = Path("replacement-domain")
    lock_a = AuthorityOwnerLock(domain).acquire()
    session_a = AuthoritySession(authority_domain=domain)
    session_a.activate()
    stale_a = session_a.authority_instance_id
    session_a.begin_drain()
    session_a.terminalize()
    lock_a.release()

    lock_b = AuthorityOwnerLock(domain).acquire()
    session_b = AuthoritySession(authority_domain=domain)
    session_b.activate()
    assert session_b.authority_instance_id != stale_a
    with pytest.raises(AuthorityStaleInstanceError):
        with session_b.admit(expected_instance_id=stale_a):
            pass
    assert session_b.active_work == 0
    with session_b.admit(expected_instance_id=session_b.authority_instance_id):
        assert session_b.active_work == 1
    session_b.begin_drain()
    session_b.terminalize()
    lock_b.release()


# ---------------------------------------------------------------------------
# 3. Controlled lifecycle qualification + session/lock semantic relationship
# ---------------------------------------------------------------------------


def test_lifecycle_is_irreversible_in_order() -> None:
    session = AuthoritySession(authority_domain=Path("x"))
    # STARTING -> DRAINING / TERMINAL skips are refused.
    with pytest.raises(AuthoritySessionError):
        session.begin_drain()
    with pytest.raises(AuthoritySessionError):
        session.terminalize()
    session.activate()
    with pytest.raises(AuthoritySessionError):
        session.activate()  # READY -> READY refused
    session.begin_drain()
    with pytest.raises(AuthoritySessionError):
        session.begin_drain()  # DRAINING -> DRAINING refused
    session.terminalize()
    # TERMINAL is absorbing: every transition out is refused.
    with pytest.raises(AuthoritySessionError):
        session.activate()
    with pytest.raises(AuthoritySessionError):
        session.begin_drain()
    session.terminalize()  # idempotent no-op on TERMINAL


def test_admitted_work_finishes_under_still_held_ownership() -> None:
    """READY→DRAINING is atomic with admission: once draining begins, no new
    admission; already-admitted work runs to completion while ownership is
    still held (the drain barrier)."""
    lock = AuthorityOwnerLock(Path("drain-domain")).acquire()
    session = AuthoritySession(authority_domain=Path("drain-domain"))
    session.activate()
    release = threading.Event()

    def work() -> None:
        with session.admit(expected_instance_id=session.authority_instance_id):
            session.begin_drain()  # legal from inside admitted work
            release.wait(timeout=5)

    thread = threading.Thread(target=work)
    thread.start()
    deadline = time.monotonic() + 3
    while session.active_work != 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.active_work == 1
    assert session.state.value == "draining"
    assert lock.held, "ownership outlives in-flight admitted work"

    release.set()
    thread.join(timeout=5)
    assert session.active_work == 0
    assert session.wait_drained(timeout=1)
    session.terminalize()
    lock.release()


def test_dispatcher_release_refused_before_terminal_session() -> None:
    """Semantic lock/session invariant, runtime-enforced: deliberate
    ownership release requires a TERMINAL (or absent) session — a release
    attempt over a live session refuses and RETAINS ownership."""
    from webwire.authority_session import AuthoritySessionState

    cfg = WebWireConfig(state_dir=Path("guard-domain").resolve(), kill_env_var=None)
    d = Dispatcher(cfg, session_manager=_StubSessionManager(cfg))  # type: ignore[arg-type]
    d._install_m5_live_stack = lambda sb: setattr(  # type: ignore[method-assign]
        d, "_m5_stack", _StubStack()
    )
    assert (asyncio_run(d.start())).ok
    assert d._authority_session is not None
    # Force a non-terminal live session and attempt deliberate release.
    d._authority_session._state = AuthoritySessionState.READY
    with pytest.raises(AuthoritySessionError, match="TERMINAL"):
        d._release_authority()
    assert d._owner_lock is not None, "refused release retains ownership"
    # Clean shutdown still works (drain -> terminal -> release).
    result = asyncio_run(d.stop())
    assert result.ok
    assert d._owner_lock is None


def test_dispatcher_exposes_instance_id_only_while_owning(tmp_path: Path) -> None:
    """No READY without ownership: while a rival holds the domain the
    runtime's start is refused busy and it has NO instance identity; once
    it owns the domain the id exists (and rides the start result); after a
    clean stop the TERMINAL session retains its id for diagnostics.
    (One Dispatcher per path per process — the coordinator registry — so
    the rival is a raw lock, the real cross-process shape.)"""
    rival = AuthorityOwnerLock(tmp_path).acquire()
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    d = Dispatcher(cfg, session_manager=_StubSessionManager(cfg))  # type: ignore[arg-type]
    d._install_m5_live_stack = lambda sb: setattr(  # type: ignore[method-assign]
        d, "_m5_stack", _StubStack()
    )
    busy = asyncio_run(d.start())
    assert busy.ok is False and "authority_busy" in busy.error.message
    assert d.authority_instance_id is None, "a loser owns no instance identity"

    rival.release()
    started = asyncio_run(d.start())
    assert started.ok
    instance_id = d.authority_instance_id
    assert instance_id and len(instance_id) == 64
    assert "authority_instance_id" in (started.data or {})
    asyncio_run(d.stop())
    assert d.authority_instance_id == instance_id  # terminal session retains it


# ---------------------------------------------------------------------------
# Shared doubles
# ---------------------------------------------------------------------------


def asyncio_run(coro):  # type: ignore[no-untyped-def]
    import asyncio

    return asyncio.run(coro)


class _StubSB:
    _page = None
    _controller = None


class _StubSessionManager(SessionManager):
    def __init__(self, config: WebWireConfig) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = True

    async def start(self) -> Any:
        return ok_result(data={})

    async def stop(self) -> Any:
        return ok_result(data={"stopped": True})


class _StubStack:
    def __init__(self) -> None:
        self.read_broker = object()


# ---------------------------------------------------------------------------
# 4. Crash/forced-death takeover, retained rendezvous, and NO stealing
# ---------------------------------------------------------------------------

_CHILD_OWNER = "\n".join(
    [
        "import asyncio, os, sys, time",
        "from pathlib import Path",
        "sys.path.insert(0, sys.argv[2])",
        "from webwire.config import WebWireConfig",
        "from webwire.dispatcher import Dispatcher",
        "from webwire.session import SessionManager",
        "",
        "class _StubSB:",
        "    _page = None",
        "    _controller = None",
        "class _SM(SessionManager):",
        "    def __init__(self, config):",
        "        super().__init__(config)",
        "        self._sb = _StubSB()",
        "        self._started = True",
        "    async def start(self):",
        "        from webwire.envelope import ok_result",
        "        return ok_result(data={})",
        "    async def stop(self):",
        "        from webwire.envelope import ok_result",
        "        return ok_result(data={'stopped': True})",
        "",
        "async def main():",
        "    cfg = WebWireConfig(state_dir=Path(sys.argv[1]), kill_env_var=None)",
        "    d = Dispatcher(cfg, session_manager=_SM(cfg))",
        "    class _Stack: read_broker = object()",
        "    d._install_m5_live_stack = lambda sb: setattr(d, '_m5_stack', _Stack())",
        "    r = await d.start()",
        "    if not r.ok:",
        "        msg = getattr(r.error, 'message', r)",
        "        print(f'START_FAILED {msg}', flush=True)",
        "        return 1",
        "    print(f'OWNER_READY instance={d.authority_instance_id}', flush=True)",
        "    release = Path(sys.argv[3])",
        "    if sys.argv[4] == 'controlled':",
        "        while not release.exists():",
        "            time.sleep(0.2)",
        "        stop = await d.stop()",
        "        print(f'OWNER_STOPPED ok={stop.ok}', flush=True)",
        "        return 0 if stop.ok else 1",
        "    # crash mode: hold until killed",
        "    deadline = time.monotonic() + 120",
        "    while time.monotonic() < deadline:",
        "        time.sleep(0.5)",
        "    return 0",
        "",
        "raise SystemExit(asyncio.run(main()))",
    ]
)

_CHILD_HUNG_HOLDER = "\n".join(
    [
        "import sys, time",
        "from pathlib import Path",
        "sys.path.insert(0, sys.argv[2])",
        "from webwire.authority import AuthorityOwnerLock",
        "lock = AuthorityOwnerLock(Path(sys.argv[1])).acquire()",
        "print('HUNG_HOLDING', flush=True)",
        "time.sleep(300)",
    ]
)


def _spawn_owner(tmp_path: Path, release: Path, mode: str):
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_OWNER, str(tmp_path), REPO_SRC, str(release), mode],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
        cwd=REPO_SRC,
    )


def test_clean_replacement_across_processes_mints_different_ids(tmp_path: Path) -> None:
    """Owner A: READY, id A, controlled drain→terminal→release. Owner B on
    the SAME canonical domain: acquires, mints id B != A."""
    release = tmp_path / "release-a"
    owner_a = _spawn_owner(tmp_path, release, "controlled")
    line = owner_a.stdout.readline().strip()
    assert line.startswith("OWNER_READY"), line
    id_a = line.split("instance=")[1]
    assert len(id_a) == 64

    release.touch()
    rest = owner_a.stdout.readline().strip()
    owner_a.wait(timeout=60)
    assert "OWNER_STOPPED ok=True" in rest

    release_b = tmp_path / "release-b"
    owner_b = _spawn_owner(tmp_path, release_b, "controlled")
    line_b = owner_b.stdout.readline().strip()
    assert line_b.startswith("OWNER_READY"), line_b
    id_b = line_b.split("instance=")[1]
    assert id_b != id_a
    release_b.touch()
    owner_b.stdout.readline()
    owner_b.wait(timeout=60)
    assert owner_b.returncode == 0


def test_forced_death_takeover_with_retained_rendezvous(tmp_path: Path) -> None:
    """Owner C READY → force-kill (no controlled shutdown) → the OS
    releases ownership, the retained authority.lock file does NOT impede
    the successor, and owner D acquires, hydrates, mints id D != C."""
    release = tmp_path / "release-c"  # never touched: crash mode
    owner_c = _spawn_owner(tmp_path, release, "crash")
    line = owner_c.stdout.readline().strip()
    assert line.startswith("OWNER_READY"), line
    id_c = line.split("instance=")[1]

    lock_file = tmp_path / "authority.lock"
    assert lock_file.exists(), "the owner's rendezvous file is present"

    owner_c.kill()  # forced death: no drain, no terminalize, no release
    assert owner_c.wait(timeout=30) != 0
    assert lock_file.exists(), "rendezvous RETAINED after death (by design)"

    # The OS released ownership: a raw acquisition succeeds over the
    # retained file.
    successor_lock = AuthorityOwnerLock(tmp_path).acquire()
    successor_lock.release()

    # Full successor runtime: acquisition + hydration + fresh id + READY.
    release_d = tmp_path / "release-d"
    owner_d = _spawn_owner(tmp_path, release_d, "controlled")
    line_d = owner_d.stdout.readline().strip()
    assert line_d.startswith("OWNER_READY"), line_d
    id_d = line_d.split("instance=")[1]
    assert id_d != id_c
    release_d.touch()
    owner_d.stdout.readline()
    owner_d.wait(timeout=60)
    assert owner_d.returncode == 0


def test_successor_hydration_is_real_not_skipped(tmp_path: Path) -> None:
    """Full hydration proof: after a crash leaves the domain free, a
    successor facing CORRUPT durable history refuses to start (hydration
    fails acquisition) rather than skipping hydration."""
    release = tmp_path / "release-x"
    owner = _spawn_owner(tmp_path, release, "crash")
    line = owner.stdout.readline().strip()
    assert line.startswith("OWNER_READY"), line
    owner.kill()
    owner.wait(timeout=30)

    (tmp_path / "effects.ndjson").write_bytes(b"\xff\xfe corrupt history")
    succ = subprocess.run(
        [sys.executable, "-c", _CHILD_OWNER, str(tmp_path), REPO_SRC, str(tmp_path / "rel2"), "controlled"],
        capture_output=True,
        text=True,
        env=_child_env(),
        cwd=REPO_SRC,
        timeout=120,
    )
    out = succ.stdout.strip()
    assert succ.returncode != 0
    assert out.startswith("START_FAILED"), out + succ.stderr[-400:]
    assert "recovery" in out.lower() or "hydrate" in out.lower()


def test_hung_owner_is_never_stolen_from(tmp_path: Path) -> None:
    """No heartbeat, TTL, PID age, lock-file age/mtime, or timeout may
    grant takeover: while owner E stays alive holding the lock, contenders
    across a bounded interval beyond any tempting lease stay busy — and
    acquisition succeeds the moment E actually terminates."""
    holder = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HUNG_HOLDER, str(tmp_path), REPO_SRC],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_child_env(),
        cwd=REPO_SRC,
    )
    try:
        assert "HUNG_HOLDING" in holder.stdout.readline()
        lock_file = tmp_path / "authority.lock"
        stat_before = lock_file.stat()
        id_before = (lock_file.stat().st_ino, lock_file.stat().st_size)

        # Bounded qualification interval: 12s of repeated contention,
        # longer than any plausible short lease. Nothing in the design
        # polls a lease, so busy must hold for the entire interval.
        deadline = time.monotonic() + 12.0
        attempts = 0
        while time.monotonic() < deadline:
            with pytest.raises(AuthorityBusyError):
                AuthorityOwnerLock(tmp_path).acquire()
            attempts += 1
            time.sleep(1.0)
        stat_after = lock_file.stat()
        assert attempts >= 10
        assert (stat_after.st_ino, stat_after.st_size) == id_before
        assert stat_after.st_mtime == stat_before.st_mtime, (
            "no lease/mtime/heartbeat writing may touch the rendezvous file"
        )

        # A full runtime contender is equally refused while E lives.
        contender = subprocess.run(
            [
                sys.executable,
                "-c",
                _CHILD_OWNER,
                str(tmp_path),
                REPO_SRC,
                str(tmp_path / "rel-e"),
                "controlled",
            ],
            capture_output=True,
            text=True,
            env=_child_env(),
            cwd=REPO_SRC,
            timeout=120,
        )
        assert contender.returncode != 0
        assert "authority_busy" in contender.stdout

        # E actually terminates → takeover becomes possible immediately.
        holder.terminate()
        holder.wait(timeout=30)
        successor = AuthorityOwnerLock(tmp_path).acquire()
        successor.release()
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=30)
