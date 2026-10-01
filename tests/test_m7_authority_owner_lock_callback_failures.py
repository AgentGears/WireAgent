"""Regressions for M7 Layer-1 fork-callback and rollback interruption handling."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from webwire.authority import AuthorityOwnerLock

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


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


def test_interrupt_after_clean_acquisition_close_completes_registry_rollback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Descriptor-free acquisition cleanup cannot strand a strong reservation."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_finalize = owner._close_and_unpublish_fd

    def fail_lock(fd: int) -> None:
        del fd
        raise OSError(errno.EIO, "injected acquisition failure")

    def finalize_then_interrupt(fd: int, *, phase: str) -> None:
        original_finalize(fd, phase=phase)
        assert owner._fd is None
        assert owner._pending_fd is None
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "_lock_fd", fail_lock)
    monkeypatch.setattr(owner, "_close_and_unpublish_fd", finalize_then_interrupt)

    with pytest.raises(KeyboardInterrupt):
        owner.acquire()

    assert owner._fd is None
    assert owner._pending_fd is None
    assert owner._release_broken is False
    assert AuthorityOwnerLock._registry.get(owner._identity) is None

    successor = AuthorityOwnerLock(state_dir).acquire()
    successor.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_parent_at_fork_release_interruption_fail_stops_process(tmp_path: Path) -> None:
    """A raising parent callback cannot return to user code with gate state unknown."""

    parent_marker = tmp_path / "parent-returned.txt"
    completed = subprocess.run(
        [
            sys.executable,
            str(_WORKER),
            "fork-parent-release-interrupt",
            str(parent_marker),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )

    assert completed.returncode == AuthorityOwnerLock._BROKEN_FORK_EXIT_CODE
    assert not parent_marker.exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_child_inherited_close_failure_fail_stops_before_user_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An ambiguous inherited-owner close never falls through to child user code."""

    state_dir = tmp_path / "state"
    child_marker = tmp_path / "child-ran.txt"
    owner = AuthorityOwnerLock(state_dir).acquire()

    def fail_close(fd: int) -> None:
        del fd
        raise OSError(errno.EIO, "injected child inherited-close failure")

    monkeypatch.setattr(
        AuthorityOwnerLock,
        "_close_inherited_fd",
        staticmethod(fail_close),
    )

    pid = os.fork()
    if pid == 0:  # pragma: no cover - fixed callback exits before user code
        child_marker.write_text("child-ran", encoding="utf-8")
        os._exit(99)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == AuthorityOwnerLock._BROKEN_FORK_EXIT_CODE
    assert not child_marker.exists()

    owner.release()
    completed, payload = _probe(state_dir)
    assert completed.returncode == 0
    assert payload["acquired"] is True
    assert payload["busy"] is False


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_child_detach_interruption_after_close_fail_stops_before_user_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A BaseException during child detach is contained by the fail-stop barrier."""

    state_dir = tmp_path / "state"
    child_marker = tmp_path / "child-ran.txt"
    owner = AuthorityOwnerLock(state_dir).acquire()
    original_close = AuthorityOwnerLock._close_inherited_fd

    def close_then_interrupt(fd: int) -> None:
        original_close(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(
        AuthorityOwnerLock,
        "_close_inherited_fd",
        staticmethod(close_then_interrupt),
    )

    pid = os.fork()
    if pid == 0:  # pragma: no cover - fixed callback exits before user code
        child_marker.write_text("child-ran", encoding="utf-8")
        os._exit(99)

    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == AuthorityOwnerLock._BROKEN_FORK_EXIT_CODE
    assert not child_marker.exists()

    owner.release()
    completed, payload = _probe(state_dir)
    assert completed.returncode == 0
    assert payload["acquired"] is True
    assert payload["busy"] is False
