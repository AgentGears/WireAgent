"""M7 Layer-1 owner-lock setup-failure resource cleanup."""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from webwire.authority import AuthorityOwnerError, AuthorityOwnerLock


def _assert_closed(fd: int) -> None:
    with pytest.raises(OSError) as excinfo:
        os.fstat(fd)
    assert excinfo.value.errno == errno.EBADF


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
