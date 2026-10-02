"""M7 Layer 2 acceptance tests — ownership is mandatory around the runtime
authority root (frozen M7_DESIGN.md §24.1: "makes ownership mandatory for
supported Dispatcher ... entrypoints and freezes request-admission/drain
ordering"; completion criteria: "losing processes fail before production
browser or safety-write authority", "request admission and owner drain have
one synchronized ordering while ownership remains held", "controlled
release is last and never overtakes admitted mutation work").

In-process topology note: a pre-existing construction-time guard (the
ReconciliationCoordinator same-path registry) already refuses two
Dispatchers on one state directory inside one process, so the runtime
contention tests represent the rival owner with a raw AuthorityOwnerLock —
exactly the cross-process topology Layer 2 polices — and at most ONE
Dispatcher is constructed per state path per process. The CLI test drives
the real production factory.

Layer-2 scope boundaries stated, not implied: this wires the supported
Dispatcher authority root and the m8 CLI's controlled busy path. Lifecycle,
stale-session denial, and the IPC topology are Layers 3+.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from webwire.authority import AuthorityOwnerLock
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.session import SessionManager


class _StubSB:
    _page = None
    _controller = None


class _RecordingSessionManager(SessionManager):
    """Session double that records lifecycle calls; start can be scripted."""

    def __init__(self, config: WebWireConfig, *, start_ok: bool = True) -> None:
        super().__init__(config)
        self._sb = _StubSB()  # type: ignore[assignment]
        self._started = False
        self._start_calls = 0
        self._stop_calls = 0
        self._start_ok = start_ok

    async def start(self) -> Any:
        self._start_calls += 1
        self._started = True
        if not self._start_ok:
            from webwire.envelope import hard_failure

            return hard_failure("stub session start refused")
        return ok_result(data={})

    async def stop(self) -> Any:
        self._stop_calls += 1
        self._started = False
        return ok_result(data={})


def _dispatcher(tmp_path: Path, *, start_ok: bool = True):
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _RecordingSessionManager(cfg, start_ok=start_ok)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    return d, sm, cfg


def _stub_the_m5_stack(monkeypatch, d: Dispatcher) -> None:
    """The live M5 stack installs over a real browser; for wiring-order
    tests the install itself is orthogonal — stub it with a minimal stack
    (a read-broker token) so start()'s retention and wiring checks pass."""
    from types import SimpleNamespace

    monkeypatch.setattr(
        d,
        "_install_m5_live_stack",
        lambda sb: setattr(d, "_m5_stack", SimpleNamespace(read_broker=object())),
    )


async def test_loser_fails_busy_before_browser_then_retries(tmp_path: Path, monkeypatch) -> None:
    """A runtime whose state directory is owned elsewhere fails start() with
    authority_busy BEFORE its browser session ever starts; once the rival
    releases, the SAME runtime retries into a clean start."""
    rival = AuthorityOwnerLock(tmp_path).acquire()
    d, sm, _ = _dispatcher(tmp_path)
    _stub_the_m5_stack(monkeypatch, d)

    r = await d.start()
    assert r.ok is False
    assert "authority_busy" in r.error.message
    assert "another WireAgent runtime owns" in r.error.message
    assert sm._start_calls == 0, "the loser must fail before its browser"
    assert sm._stop_calls == 0

    rival.release()
    r2 = await d.start()  # retry after the rival stopped: ownership is free
    assert r2.ok, getattr(r2.error, "message", r2)
    assert sm._start_calls == 1
    await d.stop()
    assert d._owner_lock is None


async def test_failed_start_releases_ownership(tmp_path: Path, monkeypatch) -> None:
    """A start that fails after acquisition (session start refused) must not
    strand the authority domain: ownership is released for a successor."""
    d, sm, _ = _dispatcher(tmp_path, start_ok=False)
    _stub_the_m5_stack(monkeypatch, d)
    r = await d.start()
    assert r.ok is False
    assert "stub session start refused" in r.error.message
    assert sm._start_calls == 1
    assert d._owner_lock is None

    # The domain is free: a rival (any successor) acquires cleanly.
    successor_lock = AuthorityOwnerLock(tmp_path).acquire()
    successor_lock.release()


async def test_double_start_is_refused_without_releasing(tmp_path: Path, monkeypatch) -> None:
    d, _sm, _ = _dispatcher(tmp_path)
    _stub_the_m5_stack(monkeypatch, d)
    r1 = await d.start()
    assert r1.ok
    r2 = await d.start()
    assert r2.ok is False
    assert "already started" in r2.error.message
    assert d._owner_lock is not None, "the refusal must not release ownership"
    await d.stop()
    assert d._owner_lock is None


async def test_stop_releases_only_after_invocations_drain(tmp_path: Path, monkeypatch) -> None:
    """Admission/drain ordering: an admitted invocation in flight when
    stop() is called completes BEFORE ownership is released; release is the
    last act and never overtakes admitted work."""
    d, _sm, _ = _dispatcher(tmp_path)
    _stub_the_m5_stack(monkeypatch, d)
    assert (await d.start()).ok

    admitted_running = asyncio.Event()
    release_admitted = asyncio.Event()
    drain_order: list[str] = []

    async def patched_invoke(name: str, input=None):  # type: ignore[no-untyped-def]
        async with d._invoke_lock:  # serialize exactly like production
            admitted_running.set()
            await release_admitted.wait()
            drain_order.append("invocation-completed")
            return ok_result(data={})

    d.invoke = patched_invoke  # type: ignore[method-assign]
    invoke_task = asyncio.create_task(patched_invoke("like_post", {}))
    await admitted_running.wait()

    stop_task = asyncio.create_task(d.stop())
    await asyncio.sleep(0.2)
    assert not stop_task.done(), "stop must wait for the admitted invocation"
    assert d._owner_lock is not None, "release is last — work still in flight"

    release_admitted.set()
    await asyncio.wait_for(asyncio.gather(invoke_task, stop_task), timeout=5)
    assert drain_order == ["invocation-completed"]
    assert d._owner_lock is None, "ownership released after the drain"
    assert invoke_task.result().ok


_CHILD_HOLD = (
    "import sys, time\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[2])\n"
    "from webwire.authority import AuthorityOwnerLock\n"
    "lock = AuthorityOwnerLock(Path(sys.argv[1])).acquire()\n"
    "print('HELD', flush=True)\n"
    "time.sleep(30)\n"
)

_CHILD_RUNTIME = (
    "import sys\n"
    "from pathlib import Path\n"
    "sys.path.insert(0, sys.argv[2])\n"
    "from webwire.config import WebWireConfig\n"
    "from webwire.dispatcher import Dispatcher\n"
    "import asyncio\n"
    "cfg = WebWireConfig(state_dir=Path(sys.argv[1]), kill_env_var=None)\n"
    "d = Dispatcher(cfg)\n"
    "r = asyncio.run(d.start())\n"
    "msg = getattr(r.error, 'message', '') if r.error else ''\n"
    "print('BUSY' if (not r.ok and 'authority_busy' in msg) else f'OTHER:{r.ok}:{msg}')\n"
)


def _child_env():
    env = dict(os.environ)
    parent_path = [os.path.abspath(e) for e in sys.path if e]
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(parent_path + ([inherited] if inherited else []))
    return env


def test_cross_process_runtime_refused_then_takeover(tmp_path: Path) -> None:
    """The real topology: a live owner process holds the state directory; a
    REAL Dispatcher in a second process is refused with authority_busy
    before its session starts; the owner dies (process death = OS-level
    release, no stale-lock-file deletion); a successor acquires."""
    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    env = _child_env()

    holder = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLD, str(tmp_path), repo_src],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        assert "HELD" in holder.stdout.readline(), "holder never acquired"
        rival = subprocess.run(
            [sys.executable, "-c", _CHILD_RUNTIME, str(tmp_path), repo_src],
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )
        assert "BUSY" in rival.stdout, f"rival runtime was not refused busy: {rival.stdout!r} {rival.stderr!r}"
        holder.kill()
        assert holder.wait(timeout=10) != 0
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)

    # OS-level takeover after process death.
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


async def test_m8_card_cli_reports_authority_busy(tmp_path, capsys) -> None:
    """The transient m8 card entrypoint loses the ownership race against a
    live runtime with a CONTROLLED message and exit code — no traceback —
    through the real production factory."""
    from webwire.m8_card_cli import CardCli, build_production_runtime
    from webwire.safety.user_rules import RuleStore

    holder = AuthorityOwnerLock(tmp_path).acquire()  # a live runtime's lock
    try:
        config = WebWireConfig(state_dir=tmp_path, kill_env_var=None)

        async def factory():
            return await build_production_runtime(config)

        cli = CardCli(
            store=RuleStore(tmp_path / "rules.json"),
            runtime_factory=factory,
            decision_reader=lambda p: "y",
            clock=lambda: 0.0,
        )
        code = await cli.run(["card", "like_post", '{"post_id": "1"}'])
        err = capsys.readouterr().err
        assert code == 1
        assert "authority_busy" in err
        assert "another WireAgent runtime owns" in err
        assert "Traceback" not in err
    finally:
        holder.release()
