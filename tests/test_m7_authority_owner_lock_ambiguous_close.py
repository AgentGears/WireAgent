"""Regressions for ambiguous owner state followed by POSIX fork."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from webwire.authority import AuthorityBusyError, AuthorityOwnerError, AuthorityOwnerLock


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_interrupt_after_kernel_close_fail_stops_later_fork_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A stale reused fd is never selectively closed by a fork child after ambiguity."""

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    assert owner._fd is not None
    owner_fd = owner._fd
    original_close = owner._close_owner_fd

    def close_then_interrupt(fd: int) -> None:
        # This is the dangerous bytecode seam: kernel close succeeds, then an
        # async exception lands before the caller can clear its published fd.
        original_close(fd)
        raise KeyboardInterrupt

    monkeypatch.setattr(owner, "_close_owner_fd", close_then_interrupt)
    with pytest.raises(AuthorityOwnerError, match="owner descriptor state is ambiguous"):
        owner.release()

    assert owner._release_broken is True
    assert owner._fd == owner_fd
    with pytest.raises(AuthorityBusyError, match="reserved by this process"):
        AuthorityOwnerLock(state_dir).acquire()

    # Force the now-closed integer to name an unrelated parent resource. A child
    # that blindly trusts owner._fd would close this inherited copy. The fixed
    # after-fork path instead exits before selective fd cleanup when any copied
    # owner is in release_broken state.
    reused_fds: list[int] = []
    reused_fd: int | None = None
    for _ in range(64):
        candidate = os.open(os.devnull, os.O_RDONLY)
        reused_fds.append(candidate)
        if candidate == owner_fd:
            reused_fd = candidate
            break
    assert reused_fd == owner_fd, "test could not force reuse of the stale owner fd number"

    child_pid = os.fork()
    if child_pid == 0:  # pragma: no cover - after-fork callback must exit first
        os._exit(99)

    _, status = os.waitpid(child_pid, 0)
    assert os.waitstatus_to_exitcode(status) == AuthorityOwnerLock._BROKEN_FORK_EXIT_CODE

    # Child exit affects only its descriptor table; the unrelated parent fd must
    # remain valid. This also proves no parent-side cleanup guessed at stale fd
    # identity after the ambiguous close.
    os.fstat(reused_fd)

    # Test-only cleanup. Production semantics require process termination after
    # release_broken; do not call close again because owner_fd now identifies the
    # unrelated resource above.
    with AuthorityOwnerLock._registry_guard:
        if AuthorityOwnerLock._registry.get(owner._identity) is owner:
            del AuthorityOwnerLock._registry[owner._identity]
    owner._fd = None
    owner._owner_pid = None
    owner._release_broken = False

    for fd in reused_fds:
        try:
            os.close(fd)
        except OSError:
            pass


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_hidden_open_state_fail_stops_fork_child_before_user_code(tmp_path: Path) -> None:
    """A child never continues while an inherited owner may hide an unpublished fd."""

    owner = AuthorityOwnerLock(tmp_path / "state")
    AuthorityOwnerLock._reserve_process_domain(owner)
    owner._opening_unpublished = True

    try:
        child_pid = os.fork()
        if child_pid == 0:  # pragma: no cover - after-fork callback must exit first
            os._exit(99)

        _, status = os.waitpid(child_pid, 0)
        assert (
            os.waitstatus_to_exitcode(status)
            == AuthorityOwnerLock._BROKEN_FORK_EXIT_CODE
        )
    finally:
        owner._opening_unpublished = False
        AuthorityOwnerLock._rollback_process_domain(owner)
