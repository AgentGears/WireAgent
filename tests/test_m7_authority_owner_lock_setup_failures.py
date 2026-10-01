"""M7 Layer-1 owner-lock setup-failure resource cleanup."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from webwire.authority import AuthorityBusyError, AuthorityOwnerError, AuthorityOwnerLock

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


class _FailStop(BaseException):
    """Test-only stand-in for the non-returning os._exit path."""


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as excinfo:
        os.fstat(fd)
    assert excinfo.value.errno == errno.EBADF


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


def test_set_inheritable_failure_closes_unpublished_fd_and_rolls_back_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    original = os.set_inheritable
    captured: list[int] = []

    def fail(fd: int, inheritable: bool) -> None:
        assert inheritable is False
        captured.append(fd)
        raise OSError(errno.EIO, "injected inheritable setup failure")

    monkeypatch.setattr(os, "set_inheritable", fail)
    with pytest.raises(AuthorityOwnerError, match="could not acquire"):
        AuthorityOwnerLock(state_dir).acquire()

    assert len(captured) == 1
    _assert_closed(captured[0])

    monkeypatch.setattr(os, "set_inheritable", original)
    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


def test_interrupt_during_inheritance_setup_does_not_strand_fd_or_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    original = os.set_inheritable
    captured: list[int] = []

    def interrupt(fd: int, inheritable: bool) -> None:
        assert inheritable is False
        captured.append(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "set_inheritable", interrupt)
    with pytest.raises(KeyboardInterrupt):
        AuthorityOwnerLock(state_dir).acquire()

    assert len(captured) == 1
    _assert_closed(captured[0])

    monkeypatch.setattr(os, "set_inheritable", original)
    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


def test_interrupt_after_registry_insert_rolls_back_descriptor_free_domain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The reservation itself is inside the acquisition rollback transaction."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_reserve = owner._reserve_process_domain

    def reserve_then_interrupt(candidate: AuthorityOwnerLock) -> None:
        original_reserve(candidate)
        assert AuthorityOwnerLock._registry.get(owner._identity) is owner
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "_reserve_process_domain", reserve_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.acquire()

    assert AuthorityOwnerLock._registry.get(owner._identity) is None
    assert owner._pending_fd is None
    assert owner._fd is None

    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


def test_hidden_open_interrupt_uses_fail_stop_instead_of_leaking_unknown_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No rollback guesses when os.open created an fd before Python could publish it."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_open = os.open
    captured: list[int] = []
    exits: list[int] = []

    def open_then_interrupt(
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
            captured.append(fd)
            # Simulate a raising signal after the kernel returned the integer but
            # before the caller could STORE_FAST/publish it.
            raise KeyboardInterrupt
        return fd

    def fail_stop(code: int) -> None:
        exits.append(code)
        raise _FailStop

    monkeypatch.setattr(os, "open", open_then_interrupt)
    monkeypatch.setattr(os, "_exit", fail_stop)

    with pytest.raises(_FailStop):
        owner.acquire()

    assert exits == [AuthorityOwnerLock._HIDDEN_OPEN_EXIT_CODE]
    assert len(captured) == 1
    assert owner._opening_unpublished is True
    assert owner._pending_fd is None
    assert owner._fd is None
    assert AuthorityOwnerLock._registry.get(owner._identity) is owner

    # Test-only cleanup because the real production path terminates here and the
    # OS closes the hidden descriptor. Restore normal os.open, close the captured
    # descriptor explicitly, and clear the copied fail-stop state.
    monkeypatch.setattr(os, "open", original_open)
    os.close(captured[0])
    owner._opening_unpublished = False
    owner._rollback_process_domain(owner)

    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


def test_interrupt_after_open_before_caller_assignment_closes_pending_fd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Cleanup derives the fd from owner state if caller STORE_FAST never occurs."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_open = owner._open_lock_file
    captured: list[int] = []

    def open_then_interrupt() -> int:
        fd = original_open()
        captured.append(fd)
        assert owner._pending_fd == fd
        # Simulate an async exception after the callee has published/returned its
        # logical result but before acquire() receives it in the local `fd` slot.
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "_open_lock_file", open_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.acquire()

    assert len(captured) == 1
    _assert_closed(captured[0])
    assert owner._pending_fd is None
    assert owner._fd is None

    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


def test_close_failure_after_os_lock_success_preserves_pending_fd_and_domain(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Ambiguous cleanup cannot hide a possibly still-held lock and free the domain."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_lock = owner._lock_fd
    original_close = owner._close_owner_fd

    def lock_then_fail(fd: int) -> None:
        # Establish the real OS lock first, then inject an acquisition-path error
        # before final held-descriptor publication.
        original_lock(fd)
        raise OSError(errno.EIO, "injected post-lock acquisition failure")

    def fail_close(fd: int) -> None:
        del fd
        # Deliberately leave the real descriptor open/locked so the test can
        # prove both process-local reservation and cross-process exclusion remain.
        raise OSError(errno.EIO, "injected pending-descriptor close failure")

    monkeypatch.setattr(owner, "_lock_fd", lock_then_fail)
    monkeypatch.setattr(owner, "_close_owner_fd", fail_close)

    with pytest.raises(AuthorityOwnerError, match="could not close pending authority owner handle"):
        owner.acquire()

    assert owner._release_broken is True
    assert owner._pending_fd is not None
    pending_fd = owner._pending_fd
    assert owner._fd is None

    with pytest.raises(AuthorityBusyError, match="reserved by this process"):
        AuthorityOwnerLock(state_dir).acquire()

    busy, payload = _probe(state_dir)
    assert busy.returncode == 23
    assert payload["acquired"] is False
    assert payload["busy"] is True

    # Test-only cleanup: the injected close did not touch the real descriptor.
    # Restore the primitive, leave the fail-stop state explicitly, close under
    # the production fork gate, then release the process-domain reservation.
    monkeypatch.setattr(owner, "_close_owner_fd", original_close)
    owner._release_broken = False
    owner._close_and_unpublish_fd(pending_fd, phase="test cleanup owner handle")
    owner._rollback_process_domain(owner)

    successor, successor_payload = _probe(state_dir)
    assert successor.returncode == 0
    assert successor_payload["acquired"] is True
    assert successor_payload["busy"] is False


def test_interrupt_after_close_unpublish_completes_registry_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A post-close async exception cannot strand a descriptor-free registry slot."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    original_finalize = owner._close_and_unpublish_fd

    def finalize_then_interrupt(fd: int, *, phase: str) -> None:
        original_finalize(fd, phase=phase)
        assert owner._fd is None
        assert owner._pending_fd is None
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "_close_and_unpublish_fd", finalize_then_interrupt)
    with pytest.raises(KeyboardInterrupt):
        owner.release()

    assert owner._fd is None
    assert owner._pending_fd is None
    assert owner._release_broken is False
    assert AuthorityOwnerLock._registry.get(owner._identity) is None

    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()
