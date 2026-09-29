"""Regressions for M7 owner-descriptor publication/unpublication vs POSIX fork."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from types import TracebackType
from typing import Optional, Type

import pytest

from webwire.authority import AuthorityOwnerLock

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


class _InstrumentedForkGuard:
    """Lock wrapper that distinguishes owner transitions from at-fork prepare.

    Production owner code enters the guard through ``with``. The registered
    at-fork prepare callback calls ``acquire()`` explicitly. That distinction
    lets the tests handshake on the exact moment fork preparation has started
    instead of relying on scheduler sleeps.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.transition_entered = threading.Event()
        self.before_fork_started = threading.Event()
        self.before_fork_acquired = threading.Event()

    def acquire(self) -> bool:
        self.before_fork_started.set()
        acquired = self._lock.acquire()
        if acquired:
            self.before_fork_acquired.set()
        return acquired

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "_InstrumentedForkGuard":
        self._lock.acquire()
        self.transition_entered.set()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._lock.release()


class _InterruptOnceForkGuard:
    """At-fork test guard whose first explicit acquire is signal-interrupted."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.acquire_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if self.acquire_calls == 1:
            raise KeyboardInterrupt
        return self._lock.acquire()

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> "_InterruptOnceForkGuard":
        self._lock.acquire()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        tb: Optional[TracebackType],
    ) -> None:
        self._lock.release()


def _probe(state_dir: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), "probe", str(state_dir)],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    stdout = completed.stdout.strip().splitlines()
    assert stdout, f"worker produced no JSON; stderr={completed.stderr!r}"
    payload = json.loads(stdout[-1])
    assert isinstance(payload, dict)
    return completed, payload


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_at_fork_prepare_retries_interrupted_guard_acquire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising signal cannot make fork proceed without actually owning the gate."""

    guard = _InterruptOnceForkGuard()
    monkeypatch.setattr(AuthorityOwnerLock, "_fork_guard", guard)

    pid = os.fork()
    if pid == 0:  # pragma: no cover - executed in fork child
        os._exit(0)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0
    assert guard.acquire_calls == 2

    # The registered parent callback must have released exactly the successful
    # second acquisition, not a sibling's lock after the interrupted first call.
    assert guard._lock.acquire(blocking=False) is True
    guard._lock.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_fork_cannot_retain_descriptor_opened_before_python_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Fork prepare waits until a kernel-opened owner fd is child-discoverable."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    guard = _InstrumentedForkGuard()
    monkeypatch.setattr(AuthorityOwnerLock, "_fork_guard", guard)

    original_open = os.open
    kernel_open_returned = threading.Event()
    allow_open_wrapper_return = threading.Event()
    acquire_done = threading.Event()
    acquire_errors: list[BaseException] = []

    def blocking_open(
        path: os.PathLike[str] | str | bytes,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if dir_fd is None:
            fd = original_open(path, flags, mode)
        else:
            fd = original_open(path, flags, mode, dir_fd=dir_fd)
        if os.fspath(path) == os.fspath(owner.lock_path):
            kernel_open_returned.set()
            if not allow_open_wrapper_return.wait(timeout=5):
                os.close(fd)
                raise RuntimeError("timed out holding pre-publication owner fd")
        return fd

    monkeypatch.setattr(os, "open", blocking_open)

    def acquire_owner() -> None:
        try:
            owner.acquire()
        except BaseException as exc:  # pragma: no cover - asserted in parent
            acquire_errors.append(exc)
        finally:
            acquire_done.set()

    acquire_thread = threading.Thread(target=acquire_owner, name="m7-owner-acquire")
    acquire_thread.start()
    assert kernel_open_returned.wait(timeout=5)
    assert guard.transition_entered.is_set()

    hold_read, hold_write = os.pipe()
    child_pid: int | None = None

    # Do not use a timer. Wait until the actual registered at-fork prepare
    # callback has attempted to acquire the guard and is therefore blocked behind
    # the acquisition thread. Only then let the os.open wrapper return so the
    # descriptor can be published as _pending_fd before fork proceeds.
    def release_open_window_after_fork_prepare() -> None:
        assert guard.before_fork_started.wait(timeout=5)
        assert not guard.before_fork_acquired.is_set()
        allow_open_wrapper_return.set()

    releaser = threading.Thread(
        target=release_open_window_after_fork_prepare,
        name="m7-open-release",
    )
    releaser.start()

    try:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - executed in fork child
            os.close(hold_write)
            try:
                os.read(hold_read, 1)
            finally:
                os.close(hold_read)
            os._exit(0)

        child_pid = pid
        os.close(hold_read)
        assert guard.before_fork_acquired.is_set()
        assert acquire_done.wait(timeout=5)
        acquire_thread.join(timeout=5)
        releaser.join(timeout=5)
        assert not acquire_thread.is_alive()
        assert not releaser.is_alive()
        assert not acquire_errors
        assert owner.held is True

        owner.release()

        # Keep the fork child alive while proving the parent's owner lock has
        # truly ended. If that child retained the pre-publication open-file
        # description, the fresh process would still report authority_busy.
        completed, payload = _probe(state_dir)
        assert completed.returncode == 0
        assert payload["acquired"] is True
        assert payload["busy"] is False
    finally:
        allow_open_wrapper_return.set()
        acquire_thread.join(timeout=5)
        releaser.join(timeout=5)
        if owner.held:
            owner.release()
        if child_pid is not None:
            try:
                os.write(hold_write, b"x")
            except OSError:
                pass
            try:
                os.close(hold_write)
            except OSError:
                pass
            _, status = os.waitpid(child_pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
        else:
            os.close(hold_read)
            os.close(hold_write)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_release_close_to_unpublish_is_atomic_against_fork_and_fd_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A fork child never closes an unrelated fd that reused a released owner number."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    assert owner._fd is not None
    owner_fd = owner._fd

    guard = _InstrumentedForkGuard()
    monkeypatch.setattr(AuthorityOwnerLock, "_fork_guard", guard)

    original_owner_close = owner._close_owner_fd
    kernel_close_done = threading.Event()
    allow_close_wrapper_return = threading.Event()
    release_done = threading.Event()
    release_errors: list[BaseException] = []

    def close_then_pause(fd: int) -> None:
        original_owner_close(fd)
        kernel_close_done.set()
        if not allow_close_wrapper_return.wait(timeout=5):
            raise RuntimeError("timed out holding close-to-unpublish owner state")

    monkeypatch.setattr(owner, "_close_owner_fd", close_then_pause)

    def release_owner() -> None:
        try:
            owner.release()
        except BaseException as exc:  # pragma: no cover - asserted in parent
            release_errors.append(exc)
        finally:
            release_done.set()

    # Allocate the child-report pipe before the owner descriptor is closed so it
    # cannot consume the soon-to-be-reused owner fd number.
    report_read, report_write = os.pipe()
    release_thread = threading.Thread(target=release_owner, name="m7-owner-release")
    release_thread.start()
    assert kernel_close_done.wait(timeout=5)
    assert guard.transition_entered.is_set()  # direct regression for Codex P1 follow-up

    reused_fds: list[int] = []
    reused_fd: int | None = None
    for _ in range(64):
        candidate = os.open(os.devnull, os.O_RDONLY)
        reused_fds.append(candidate)
        if candidate == owner_fd:
            reused_fd = candidate
            break
    assert reused_fd == owner_fd, "test could not force reuse of the closed owner fd number"

    child_pid: int | None = None

    # As above, handshake on the real at-fork prepare callback. The release
    # thread is paused after kernel close but still holds the production fork
    # guard; fork must not snapshot the stale _fd integer. Let release finish
    # only after before_fork is demonstrably waiting on that guard.
    def finish_release_after_fork_prepare() -> None:
        assert guard.before_fork_started.wait(timeout=5)
        assert not guard.before_fork_acquired.is_set()
        allow_close_wrapper_return.set()

    finisher = threading.Thread(
        target=finish_release_after_fork_prepare,
        name="m7-close-release",
    )
    finisher.start()

    try:
        pid = os.fork()
        if pid == 0:  # pragma: no cover - executed in fork child
            os.close(report_read)
            try:
                os.fstat(reused_fd)
            except OSError:
                payload = b"closed"
            else:
                payload = b"open"
            os.write(report_write, payload)
            os.close(report_write)
            os._exit(0)

        child_pid = pid
        os.close(report_write)
        assert guard.before_fork_acquired.is_set()
        assert release_done.wait(timeout=5)
        release_thread.join(timeout=5)
        finisher.join(timeout=5)
        assert not release_thread.is_alive()
        assert not finisher.is_alive()
        assert not release_errors
        assert owner._fd is None

        child_payload = os.read(report_read, 32)
        _, status = os.waitpid(child_pid, 0)
        child_pid = None
        assert os.waitstatus_to_exitcode(status) == 0
        assert child_payload == b"open"
    finally:
        allow_close_wrapper_return.set()
        release_thread.join(timeout=5)
        finisher.join(timeout=5)
        if owner.held:
            monkeypatch.setattr(owner, "_close_owner_fd", original_owner_close)
            owner.release()
        try:
            os.close(report_read)
        except OSError:
            pass
        try:
            os.close(report_write)
        except OSError:
            pass
        for fd in reused_fds:
            try:
                os.close(fd)
            except OSError:
                pass
        if child_pid is not None:
            _, status = os.waitpid(child_pid, 0)
            assert os.waitstatus_to_exitcode(status) == 0
