"""M7 Layer-1 AuthorityOwnerLock and canonical-domain qualification."""

from __future__ import annotations

import errno
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from webwire.authority import (
    AuthorityBusyError,
    AuthorityOwnerError,
    AuthorityOwnerLock,
    AuthorityStateError,
    canonical_authority_domain,
)

_WORKER = Path(__file__).with_name("_m7_authority_worker.py")


def _probe(state_dir: Path) -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
    completed = subprocess.run(
        [sys.executable, str(_WORKER), "probe", str(state_dir)],
        check=False,
        capture_output=True,
        text=True,
    )
    stdout = completed.stdout.strip().splitlines()
    assert stdout, f"worker produced no JSON; stderr={completed.stderr!r}"
    payload = json.loads(stdout[-1])
    assert isinstance(payload, dict)
    return completed, payload


def test_canonical_domain_freezes_absolute_path_at_construction(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    first_cwd = tmp_path / "first"
    second_cwd = tmp_path / "second"
    first_cwd.mkdir()
    second_cwd.mkdir()
    monkeypatch.chdir(first_cwd)

    lock = AuthorityOwnerLock(Path("state"))
    expected = (first_cwd / "state").resolve(strict=False)
    assert lock.authority_domain == expected
    assert canonical_authority_domain(Path("state")) == expected

    monkeypatch.chdir(second_cwd)
    lock.acquire()
    try:
        assert lock.authority_domain == expected
        assert lock.lock_path == expected / "authority.lock"
        assert lock.lock_path.exists()
    finally:
        lock.release()


def test_same_process_duplicate_domain_is_denied(tmp_path: Path) -> None:
    first = AuthorityOwnerLock(tmp_path / "state")
    second = AuthorityOwnerLock(tmp_path / "state")

    first.acquire()
    try:
        with pytest.raises(AuthorityBusyError, match="authority_busy"):
            second.acquire()
    finally:
        first.release()


def test_registry_uses_normcase_identity_before_os_lock(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(os.path, "normcase", lambda value: value.casefold())
    first = AuthorityOwnerLock(tmp_path / "CaseDomain")
    second = AuthorityOwnerLock(tmp_path / "casedomain")

    first.acquire()
    try:
        with pytest.raises(AuthorityBusyError, match="reserved by this process"):
            second.acquire()
        assert not (tmp_path / "casedomain").exists()
    finally:
        first.release()


def test_os_lock_excludes_fresh_process_then_allows_clean_successor(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    try:
        busy, busy_payload = _probe(state_dir)
        assert busy.returncode == 23
        assert busy_payload["acquired"] is False
        assert busy_payload["busy"] is True
    finally:
        owner.release()

    successor, successor_payload = _probe(state_dir)
    assert successor.returncode == 0
    assert successor_payload["acquired"] is True
    assert successor_payload["busy"] is False


def test_rendezvous_file_existence_is_not_authority_and_file_is_retained(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    rendezvous = state_dir / "authority.lock"
    rendezvous.touch()

    lock = AuthorityOwnerLock(state_dir)
    lock.acquire()
    lock.release()

    assert rendezvous.exists()
    successor, payload = _probe(state_dir)
    assert successor.returncode == 0
    assert payload["acquired"] is True


def test_unrelated_descriptor_close_does_not_release_owner_lock(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    try:
        unrelated = os.open(owner.lock_path, os.O_RDONLY)
        os.close(unrelated)

        busy, payload = _probe(state_dir)
        assert busy.returncode == 23
        assert payload["busy"] is True
    finally:
        owner.release()


def test_owner_descriptor_is_non_inheritable(tmp_path: Path) -> None:
    lock = AuthorityOwnerLock(tmp_path / "state").acquire()
    try:
        assert lock._fd is not None
        assert os.get_inheritable(lock._fd) is False
    finally:
        lock.release()


def test_open_failure_rolls_back_process_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    broken = AuthorityOwnerLock(state_dir)

    def fail_open() -> int:
        raise PermissionError(errno.EACCES, "injected open failure")

    monkeypatch.setattr(broken, "_open_lock_file", fail_open)
    with pytest.raises(AuthorityOwnerError, match="could not acquire"):
        broken.acquire()

    successor = AuthorityOwnerLock(state_dir)
    successor.acquire()
    successor.release()


def test_non_contention_os_lock_error_rolls_back_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    broken = AuthorityOwnerLock(state_dir)

    def fail_lock(fd: int) -> None:
        del fd
        raise OSError(errno.EIO, "injected lock failure")

    monkeypatch.setattr(broken, "_lock_fd", fail_lock)
    with pytest.raises(AuthorityOwnerError, match="could not acquire"):
        broken.acquire()

    successor = AuthorityOwnerLock(state_dir)
    successor.acquire()
    successor.release()


def test_close_failure_keeps_same_process_domain_reserved_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    original_close = owner._close_owner_fd

    def fail_close(fd: int) -> None:
        del fd
        raise OSError(errno.EIO, "injected close failure")

    monkeypatch.setattr(owner, "_close_owner_fd", fail_close)
    with pytest.raises(AuthorityOwnerError, match="could not close"):
        owner.release()

    assert owner.held is False
    with pytest.raises(AuthorityBusyError, match="reserved by this process"):
        AuthorityOwnerLock(state_dir).acquire()

    # The injected failure did not actually close the real descriptor; restore
    # the real close primitive so the test can clean up without weakening the
    # production fail-stop rule.
    owner._release_broken = False
    monkeypatch.setattr(owner, "_close_owner_fd", original_close)
    owner.release()


def test_double_acquire_and_release_without_ownership_fail_closed(tmp_path: Path) -> None:
    lock = AuthorityOwnerLock(tmp_path / "state")
    with pytest.raises(AuthorityStateError, match="not held"):
        lock.release()

    lock.acquire()
    try:
        with pytest.raises(AuthorityStateError, match="already acquired"):
            lock.acquire()
    finally:
        lock.release()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork qualification only")
def test_fork_child_detaches_inherited_descriptor_without_unlocking_parent(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    owner = AuthorityOwnerLock(state_dir).acquire()
    read_fd, write_fd = os.pipe()

    pid = os.fork()
    if pid == 0:  # pragma: no cover - executed in fork child
        os.close(read_fd)
        payload: dict[str, object] = {"held": owner.held}
        try:
            owner.release()
        except AuthorityStateError:
            payload["release_denied"] = True
        else:
            payload["release_denied"] = False
        os.write(write_fd, json.dumps(payload).encode("utf-8"))
        os.close(write_fd)
        os._exit(0)

    os.close(write_fd)
    try:
        raw = os.read(read_fd, 65536)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
        payload = json.loads(raw.decode("utf-8"))
        assert payload == {"held": False, "release_denied": True}

        busy, busy_payload = _probe(state_dir)
        assert busy.returncode == 23
        assert busy_payload["busy"] is True
    finally:
        os.close(read_fd)
        owner.release()
