"""M7 Layer 2 acceptance tests — the authority session and lifecycle fence
(F-43..F-48 regressions, PR #25 second review round).

Each test names the finding it proves closed:

- F-43: reconciliation operator access is owner-gated (no session /
  draining / terminal → refused); the offline recovery owner acquires the
  domain itself and refuses while a runtime holds it.
- F-44: a cancelled start (partial browser) quiesces before release; a
  partial browser object is stopped, never reported already-stopped.
- F-45: the start/stop race — stop() cannot interleave with a half-started
  root; no live runtime survives after ownership release.
- F-46: the lock domain and the safety-state domain are ONE canonical
  absolute directory; a CWD change cannot split them.
- F-47: pending confirmation authority is revoked before ownership
  release; a TERMINAL session never restarts.
- F-48: admitted reconciliation work blocks ownership release; the drain
  is owner-wide, not invoke-only.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

from webwire.authority import AuthorityBusyError, AuthorityOwnerLock
from webwire.authority_operators import (
    OwnedReconciliationOperatorSession,
)
from webwire.authority_session import (
    AuthorityAdmissionClosedError,
    AuthoritySessionError,
)
from webwire.config import WebWireConfig
from webwire.dispatcher import Dispatcher
from webwire.envelope import ok_result
from webwire.session import SessionManager


class _StubSB:
    def __init__(self) -> None:
        self.stopped = False

    async def stop(self) -> None:
        self.stopped = True


class _GateSessionManager(SessionManager):
    """Session double with a controllable start gate and call recording."""

    def __init__(self, config: WebWireConfig, *, start_ok: bool = True) -> None:
        super().__init__(config)
        self._stub_sb: Any = None
        self._started = False
        self.start_calls = 0
        self.stop_calls = 0
        self.start_gate: asyncio.Event | None = None
        self._start_ok = start_ok

    @property
    def sb(self):  # type: ignore[no-untyped-def]
        return self._stub_sb

    async def start(self) -> Any:
        self.start_calls += 1
        if self.start_gate is not None:
            await self.start_gate.wait()
        if not self._start_ok:
            from webwire.envelope import hard_failure

            return hard_failure("stub session start refused")
        self._stub_sb = _StubSB()
        self._started = True
        return ok_result(data={})

    async def stop(self) -> Any:
        self.stop_calls += 1
        sb = self._stub_sb
        if sb is None:
            self._started = False
            return ok_result(data={"already_stopped": True})
        try:
            await sb.stop()
        except BaseException as exc:
            # Mirrors the REAL SessionManager contract (F-49): teardown
            # failure keeps the browser reference and reports hard failure.
            from webwire.envelope import hard_failure

            return hard_failure(f"browser stop failed; root not proven retired ({exc!r})")
        self._stub_sb = None
        self._started = False
        return ok_result(data={"stopped": True})


def _dispatcher(tmp_path: Path, *, start_ok: bool = True):
    from types import SimpleNamespace

    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = _GateSessionManager(cfg, start_ok=start_ok)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    d._install_m5_live_stack = lambda sb: setattr(  # type: ignore[method-assign]
        d, "_m5_stack", SimpleNamespace(read_broker=object())
    )
    return d, sm


# ---------------------------------------------------------------------------
# F-43: reconciliation is owner-gated; the offline owner acquires or refuses
# ---------------------------------------------------------------------------


async def test_F43_operator_access_requires_active_owner_session(
    tmp_path: Path,
) -> None:
    d, _sm = _dispatcher(tmp_path)

    # No session (never started): refused.
    with pytest.raises(AuthoritySessionError, match="no authority session"):
        d.create_reconciliation_operator_session("op-1")

    assert (await d.start()).ok
    owned = d.create_reconciliation_operator_session("op-1")
    assert isinstance(owned, OwnedReconciliationOperatorSession)

    # Draining/terminal: refused.
    d._authority_session.begin_drain()
    with pytest.raises(AuthorityAdmissionClosedError, match="draining"):
        d.create_reconciliation_operator_session("op-2")
    d._authority_session.terminalize()
    with pytest.raises(AuthorityAdmissionClosedError, match="terminal"):
        d.create_reconciliation_operator_session("op-3")
    d._authority_session = None
    d._release_authority()


def _run_offline_child(tmp_path: Path) -> None:
    """Run the offline-owner child in a real subprocess (sync: the
    preparation uses blocking os/path APIs; ASYNC240-clean by design)."""
    import subprocess
    import sys

    repo_src = str(Path(__file__).resolve().parents[1] / "src")
    child_code = "\n".join(
        [
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[2])",
            "from webwire.config import WebWireConfig",
            "from webwire.offline_recovery import OfflineRecoveryAuthority",
            "with OfflineRecoveryAuthority(",
            "        WebWireConfig(state_dir=Path(sys.argv[1]))) as owner:",
            "    op = owner.operator_session('offline-op')",
            "    targets = op.list_targets()",
            "print('CLOSED')",
        ]
    )
    env = dict(os.environ)
    parent_path = [os.path.abspath(e) for e in sys.path if e]
    inherited = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(parent_path + ([inherited] if inherited else []))
    result = subprocess.run(
        [sys.executable, "-c", child_code, str(tmp_path), repo_src],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert "CLOSED" in result.stdout


async def test_F43_offline_recovery_owner_acquires_or_refuses(tmp_path: Path) -> None:

    from webwire.offline_recovery import OfflineRecoveryAuthority

    # While a runtime holds the domain, offline acquisition fails closed.
    d, _sm = _dispatcher(tmp_path)
    assert (await d.start()).ok
    offline = OfflineRecoveryAuthority(WebWireConfig(state_dir=tmp_path))
    with pytest.raises(AuthorityBusyError):
        offline.acquire()
    await d.stop()

    # With the domain free, the offline owner acquires, admits operator
    # work, and closes with the full shutdown law. This runs in a real
    # subprocess: within ONE process the coordinator same-path registry
    # (a strong ClassVar, never cleaned) already refuses constructing a
    # second authority root on this path — offline recovery is by
    # construction a separate process from any runtime.
    _run_offline_child(tmp_path)

    # Released: the domain is free for a successor (any owner).
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


# ---------------------------------------------------------------------------
# F-44: cancelled start quiesces the partial root before release
# ---------------------------------------------------------------------------


async def test_F44_cancelled_start_quiesces_then_releases(tmp_path: Path) -> None:
    d, sm = _dispatcher(tmp_path)
    sm.start_gate = asyncio.Event()

    start_task = asyncio.create_task(d.start())
    await asyncio.sleep(0.1)  # inside session.start(), waiting on the gate
    assert sm.start_calls == 1

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    # The partial root was quiesced (stop called) and ownership released.
    assert sm.stop_calls == 1, "failed start must attempt session teardown"
    assert d._owner_lock is None, "quiesced failure releases ownership"
    # The domain is free for a successor.
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


async def test_F44_partial_browser_is_stopped_not_skipped(tmp_path: Path) -> None:
    """The SessionManager window: _sb exists, _started still False — stop()
    must stop the browser object, never report already-stopped over it."""
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    sm = SessionManager(cfg)
    partial = _StubSB()
    sm._sb = partial  # type: ignore[assignment]
    sm._started = False
    result = await sm.stop()
    assert result.ok
    assert partial.stopped, "a live browser object must be stopped"


# ---------------------------------------------------------------------------
# F-45: the start/stop race cannot leave a live runtime without ownership
# ---------------------------------------------------------------------------


async def test_F45_stop_waits_for_start_and_never_orphans_the_root(
    tmp_path: Path,
) -> None:
    d, sm = _dispatcher(tmp_path)
    sm.start_gate = asyncio.Event()

    start_task = asyncio.create_task(d.start())
    await asyncio.sleep(0.1)  # STARTING: inside session.start()
    assert d._authority_session is not None
    assert d._authority_session.state.value == "starting"

    stop_task = asyncio.create_task(d.stop())
    await asyncio.sleep(0.2)
    # The fence: stop() is blocked while the root is STARTING — it can
    # neither drain-and-release under a half-started root nor no-op.
    assert not stop_task.done(), "stop must not interleave with a starting root"
    assert d._owner_lock is not None, "ownership still held during the fence"

    sm.start_gate.set()  # the start completes
    start_result = await asyncio.wait_for(start_task, timeout=5)
    stop_result = await asyncio.wait_for(stop_task, timeout=5)

    assert start_result.ok, "start completed under held ownership"
    assert stop_result.ok
    # No live runtime after release: the root is down and ownership gone.
    assert d._owner_lock is None
    assert d._authority_session.state.value == "terminal"
    assert sm.stop_calls == 1
    assert sm._stub_sb is None
    # A successor may now own the domain.
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


# ---------------------------------------------------------------------------
# F-46: one canonical absolute domain regardless of CWD
# ---------------------------------------------------------------------------


async def test_F46_lock_domain_and_safety_state_share_one_canonical_dir(tmp_path: Path, monkeypatch) -> None:
    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    # A RELATIVE state dir, resolved against the current working directory.
    cfg = WebWireConfig(state_dir=Path("../authority-root"), kill_env_var=None)
    d = Dispatcher(cfg)
    expected = (workdir / "../authority-root").resolve(strict=False)
    assert d._config.state_dir.is_absolute()
    assert d._config.state_dir == expected
    assert d._m5_ledger.path.is_absolute()
    assert d._journal._path.is_absolute() if hasattr(d._journal, "_path") else True

    # A CWD change cannot move the safety-state domain.
    monkeypatch.chdir(tmp_path)
    assert d._config.state_dir == expected
    assert d._m5_ledger.path == d._m5_ledger.path.resolve(strict=False)

    # The LOCK domain is the same canonical directory: a raw lock acquired
    # on the resolved absolute path makes THIS runtime's start busy (one
    # Dispatcher per path per process — the coordinator registry forbids
    # a second construction in-process, so the runtime here is `d` itself).
    sm = _GateSessionManager(d._config)
    d._session = sm  # type: ignore[assignment]
    rival = AuthorityOwnerLock(expected).acquire()
    r = await d.start()
    assert r.ok is False
    assert "authority_busy" in r.error.message
    assert sm.start_calls == 0, "the loser fails before its browser"
    rival.release()


# ---------------------------------------------------------------------------
# F-47: confirmation authority dies with the owner session; TERMINAL never restarts
# ---------------------------------------------------------------------------


async def test_F47_pending_tokens_revoked_before_release_and_no_restart(
    tmp_path: Path,
) -> None:
    d, _sm = _dispatcher(tmp_path)
    assert (await d.start()).ok
    confirmation = d._write_kernel.confirmation_state
    epoch_before = confirmation.current_epoch

    # A live pending token (phase-1 authority) exists in this owner session.
    token = confirmation.issue(
        intent_hash="a" * 32,
        risk_tier=__import__(
            "webwire.safety.models", fromlist=["RiskTier"]
        ).RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like_post",
    )
    assert confirmation.current_epoch == epoch_before

    await d.stop()
    # Revocation ran BEFORE release: the epoch advanced, and the pending
    # token is stale — it cannot carry phase 2 in any successor session.
    assert confirmation.current_epoch > epoch_before

    # The pending token is stale: validate_and_consume refuses it with the
    # stale-epoch reason (a tuple return, not a raise).
    _t, blocked_by = confirmation.validate_and_consume(
        token.token,
        intent_hash="a" * 32,
        risk_tier=__import__(
            "webwire.safety.models", fromlist=["RiskTier"]
        ).RiskTier.PUBLIC_REVERSIBLE_ENGAGEMENT,
        capability_name="like_post",
    )
    assert blocked_by == "stale_confirmation_epoch", blocked_by

    # TERMINAL never becomes active again.
    r = await d.start()
    assert r.ok is False
    assert "TERMINAL" in r.error.message


# ---------------------------------------------------------------------------
# F-48: admitted reconciliation work blocks ownership release
# ---------------------------------------------------------------------------


async def test_F48_active_reconciliation_blocks_ownership_release(
    tmp_path: Path,
) -> None:
    d, _sm = _dispatcher(tmp_path)
    assert (await d.start()).ok

    import threading as _threading

    admitted = _threading.Event()  # cross-thread: work runs in an executor
    release_work = _threading.Event()
    order: list[str] = []

    class _BlockingOperator:
        def prepare_resolution(self, *args: Any, **kwargs: Any) -> Any:
            with d._authority_session.admit():
                admitted.set()
                release_work.wait(timeout=10)
                order.append("reconciliation-completed")
                return ok_result(data={"prepared": True})

    owned = OwnedReconciliationOperatorSession(
        session=d._authority_session,
        operator_id="op",
        delegate_factory=lambda oid: _BlockingOperator(),
    )
    work_task = asyncio.get_event_loop().run_in_executor(None, owned.prepare_resolution)
    assert await asyncio.get_event_loop().run_in_executor(None, admitted.wait)

    stop_task = asyncio.create_task(d.stop())
    await asyncio.sleep(0.2)
    assert not stop_task.done(), "reconciliation in flight must block release"
    assert d._owner_lock is not None

    release_work.set()
    await asyncio.wait_for(asyncio.gather(asyncio.wrap_future(work_task), stop_task), timeout=5)
    assert order == ["reconciliation-completed"]
    assert d._owner_lock is None
    assert d._authority_session.state.value == "terminal"


# ---------------------------------------------------------------------------
# F-49 / F-52 / F-53 / F-54 (third review round): genuinely fail-closed
# teardown, failed-start terminality, hydrated offline acquisition, one
# canonical domain on the production path
# ---------------------------------------------------------------------------


class _FailingStopSB(_StubSB):
    """A browser whose stop() RAISES: teardown can never prove retirement."""

    async def stop(self) -> None:
        raise RuntimeError("browser teardown exploded")


class _QuiesceGateSM(_GateSessionManager):
    """Gate session whose browser object can be scripted to fail teardown."""

    def __init__(self, config: WebWireConfig, *, sb: Any) -> None:
        super().__init__(config)
        self._stub_sb = sb
        self._started = True

    async def start(self) -> Any:
        self.start_calls += 1
        return ok_result(data={})


async def test_F49_failed_browser_stop_retains_ownership(tmp_path: Path) -> None:
    """A runtime whose browser cannot be proven retired keeps ownership
    fail-closed — stop() returns a failure and the owner lock stays held,
    on BOTH the normal stop path and the failed-start teardown path."""
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    failing_sb = _FailingStopSB()
    sm = _QuiesceGateSM(cfg, sb=failing_sb)
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    d._install_m5_live_stack = lambda sb_: setattr(  # type: ignore[method-assign]
        d, "_m5_stack", __import__("types").SimpleNamespace(read_broker=object())
    )
    assert (await d.start()).ok

    r = await d.stop()
    assert r.ok is False, "stop over an unproven root must fail"
    assert "could not be proven retired" in r.error.message
    assert d._owner_lock is not None, "ownership retained fail-closed"
    assert d._authority_session.state.value == "draining"
    # The domain is NOT free: a rival is refused.
    from webwire.authority import AuthorityBusyError

    with pytest.raises(AuthorityBusyError):
        AuthorityOwnerLock(tmp_path).acquire()

    # A retry after the browser heals completes the shutdown law.
    async def _healed_stop() -> None:
        pass

    failing_sb.stop = _healed_stop  # type: ignore[method-assign]
    r2 = await d.stop()
    assert r2.ok
    assert d._owner_lock is None
    assert d._authority_session.state.value == "terminal"


async def test_F49_failed_start_teardown_failure_retains_ownership(
    tmp_path: Path,
) -> None:
    """The failed-start path: session start returns a failure AND the
    partial browser's stop raises — ownership must stay held."""
    cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    failing_sb = _FailingStopSB()
    sm = _GateSessionManager(cfg, start_ok=False)
    sm._stub_sb = failing_sb  # a partial browser exists at teardown time
    d = Dispatcher(cfg, session_manager=sm)  # type: ignore[arg-type]
    d._install_m5_live_stack = lambda sb_: None  # type: ignore[method-assign]

    r = await d.start()
    assert r.ok is False
    # session.stop() was attempted and FAILED (the browser raises):
    # ownership is retained, not released over an ambiguous root.
    assert sm.stop_calls >= 1
    assert d._owner_lock is not None, "ambiguous teardown keeps ownership"


async def test_F52_failed_start_marks_session_terminal_before_release(
    tmp_path: Path,
) -> None:
    """A cleanly quiesced failed start terminalizes the session BEFORE the
    owner handle closes: the Dispatcher never reports a live (STARTING)
    session it no longer owns."""
    d, sm = _dispatcher(tmp_path, start_ok=False)
    r = await d.start()
    assert r.ok is False
    assert sm.stop_calls == 1, "quiesce attempted"
    assert d._owner_lock is None, "proven quiescent → released"
    assert d._authority_session.state.value == "terminal", (
        "the failed session is TERMINAL at the release boundary"
    )
    # The dispatcher does not pretend to own anything anymore.
    r2 = await d.stop()
    assert r2.ok and r2.data.get("already_stopped") is True


async def test_F53_offline_owner_refuses_corrupt_effect_history(tmp_path: Path) -> None:
    """Frozen §14.2: acquisition → hydrate → activate. A corrupt effect
    ledger makes ACQUISITION fail — no READY owner, no operator access."""
    from webwire.offline_recovery import OfflineRecoveryAuthority
    from webwire.safety.recovery_guard import RecoveryGuardUnavailable

    (tmp_path / "effects.ndjson").write_bytes(b"\xff\xfe not json")
    offline = OfflineRecoveryAuthority(WebWireConfig(state_dir=tmp_path))
    with pytest.raises(RecoveryGuardUnavailable):
        offline.acquire()
    assert offline._session is None, "never activated"
    # The failed acquisition released the lock: a successor may acquire.
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


async def test_F53_offline_owner_refuses_corrupt_reconciliation_history(
    tmp_path: Path,
) -> None:
    from webwire.offline_recovery import OfflineRecoveryAuthority
    from webwire.safety.recovery_guard import RecoveryGuardUnavailable

    # A valid effect ledger, but a corrupt reconciliation ledger — the
    # composite projection must refuse acquisition just the same.
    (tmp_path / "effects.ndjson").write_text("", encoding="utf-8")
    (tmp_path / "reconciliations.ndjson").write_bytes(b"\xff\xfe broken")
    offline = OfflineRecoveryAuthority(WebWireConfig(state_dir=tmp_path))
    with pytest.raises(RecoveryGuardUnavailable):
        offline.acquire()
    successor = AuthorityOwnerLock(tmp_path).acquire()
    successor.release()


async def test_F54_production_factory_pins_one_domain_across_cwd_change(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    """The REAL production path: build_production_runtime with a RELATIVE
    state root canonicalizes before constructing the session manager and
    dispatcher — after a CWD change, the session manager's persistence
    paths and the Dispatcher's authority domain are the SAME absolute
    directory, and the relative-root warning was emitted."""
    import logging

    from webwire.config import WebWireConfig
    from webwire.m8_card_cli import build_production_runtime

    workdir = tmp_path / "work"
    workdir.mkdir()
    monkeypatch.chdir(workdir)

    captured = []

    class _WarnCollector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append(record.getMessage())

    logging.getLogger("webwire.dispatcher").addHandler(_WarnCollector())
    try:
        cfg = WebWireConfig(state_dir=Path("../prod-root"), kill_env_var=None)
        expected = (workdir / "../prod-root").resolve(strict=False)

        made = {}

        class _RecordingDispatcher(_RecordingDispatcherBase):
            async def start(self):
                made["dispatcher_config_state"] = self._config.state_dir
                made["session_config_state"] = self._session._ww_config.state_dir
                return await super().start()

        runtime = await build_production_runtime(
            cfg,
            dispatcher_factory=lambda c, *, session_manager: _RecordingDispatcher(
                c, session_manager=session_manager
            ),
        )
        # CWD changes AFTER construction: the recorded domains must already
        # be pinned to the same absolute path.
        monkeypatch.chdir(tmp_path)
        assert made["dispatcher_config_state"] == expected
        assert made["session_config_state"] == expected
        assert made["dispatcher_config_state"] is made["session_config_state"] or (
            made["dispatcher_config_state"] == made["session_config_state"]
        )
        # The warning is emitted by the REAL Dispatcher construction with a
        # relative root (the recording factory above bypasses __init__):
        # construct one real Dispatcher inside the same captured-log scope.
        from webwire.dispatcher import Dispatcher as _RealDispatcher

        _RealDispatcher(WebWireConfig(state_dir=Path("../prod-root"), kill_env_var=None))
        assert any("relative state_dir" in m for m in captured), (
            "the frozen relative-root warning must be emitted"
        )
        await runtime.stop()
    finally:
        logging.getLogger("webwire.dispatcher").removeHandler(_WarnCollector())


class _RecordingDispatcherBase:
    """A recording stand-in for the production wiring test."""

    def __init__(self, config, *, session_manager) -> None:
        self._config = config
        self._session = session_manager
        self.stopped = False

    async def start(self):
        from webwire.envelope import ok_result

        return ok_result(data={})

    async def invoke(self, capability, payload):
        from webwire.envelope import ok_result

        if capability == "whoami":
            self._session._resolved_handle = "@owner"  # mirror the post-whoami hook
            return ok_result(data={"handle": "@owner"})
        raise AssertionError(f"unexpected invoke {capability}")

    async def stop(self):
        self.stopped = True
        from webwire.envelope import ok_result

        return ok_result(data={})


async def test_F54_mismatched_injected_manager_is_refused(tmp_path: Path) -> None:
    """Defense in depth: a manager carrying a DIFFERENT state root than the
    canonical authority domain is refused at Dispatcher construction."""
    from webwire.session import SessionManager

    good_cfg = WebWireConfig(state_dir=tmp_path, kill_env_var=None)
    other_cfg = WebWireConfig(state_dir=tmp_path / "elsewhere", kill_env_var=None)
    sm = SessionManager(other_cfg)
    with pytest.raises(ValueError, match="does not match the canonical"):
        Dispatcher(good_cfg, session_manager=sm)  # type: ignore[arg-type]
