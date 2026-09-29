"""Regression for the M7 owner-lock fork pre-publication window."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from webwire.authority import AuthorityOwnerLock

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_fork_cannot_retain_descriptor_opened_before_python_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A sibling-thread fork cannot capture an owner fd hidden from at-fork cleanup.

    The injected os.open wrapper pauses *after* the kernel has returned the new
    descriptor but before AuthorityOwnerLock can publish it. M7's fork gate must
    prevent the main thread from completing fork across that interval. The fork
    child is deliberately kept alive after the parent releases ownership; a
    fresh process must still acquire immediately, proving the child did not keep
    the parent's flock/open-file-description alive.
    """

    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir)
    original_open = os.open
    open_returned = threading.Event()
    allow_python_return = threading.Event()
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
            open_returned.set()
            if not allow_python_return.wait(timeout=5):
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
    assert open_returned.wait(timeout=5)

    hold_read, hold_write = os.pipe()
    child_pid: int | None = None

    # Release the injected os.open pause only after fork has had enough time to
    # enter its before-fork callback. With the required gate, fork cannot return
    # before this event becomes set. Without the gate, it snapshots the hidden fd.
    def release_open_window() -> None:
        time.sleep(0.15)
        allow_python_return.set()

    releaser = threading.Thread(target=release_open_window, name="m7-open-release")
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

        # If fork returned while the os.open wrapper was still paused, the fd was
        # inherited before AuthorityOwnerLock could publish it to the child hook.
        assert allow_python_return.is_set()
        assert acquire_done.wait(timeout=5)
        acquire_thread.join(timeout=5)
        releaser.join(timeout=5)
        assert not acquire_thread.is_alive()
        assert not acquire_errors
        assert owner.held is True

        owner.release()

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
        assert completed.returncode == 0
        assert payload["acquired"] is True
        assert payload["busy"] is False
    finally:
        allow_python_return.set()
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
