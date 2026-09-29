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
