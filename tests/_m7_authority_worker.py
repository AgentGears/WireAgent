"""Fresh-process probes for M7 Layer-1 AuthorityOwnerLock tests."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

# Match tests/conftest.py for CI's --no-deps editable install. Importing a
# webwire submodule first imports the package, whose public surface references
# Dispatcher/Super-Browser.
try:
    import super_browser  # noqa: F401
except ImportError:  # pragma: no cover - CI-only subprocess path
    sys.path.insert(0, str(Path(__file__).parent / "stubs"))

from webwire.authority import AuthorityBusyError, AuthorityOwnerLock  # noqa: E402


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, sort_keys=True), flush=True)


def _payload(lock: AuthorityOwnerLock, *, acquired: bool, busy: bool) -> dict[str, object]:
    return {
        "acquired": acquired,
        "busy": busy,
        "domain": str(lock.authority_domain),
        "lock_path": str(lock.lock_path),
    }


def _wait_for(path: Path, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError(f"timed out waiting for {path}")
        time.sleep(0.01)


def probe(state_dir: Path) -> int:
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _emit(_payload(lock, acquired=False, busy=True))
        return 23

    try:
        _emit(_payload(lock, acquired=True, busy=False))
        return 0
    finally:
        lock.release()


def race(
    state_dir: Path,
    start_gate: Path,
    release_gate: Path,
    result_path: Path,
) -> int:
    """Race a sibling for ownership and keep the winner held until released."""

    _wait_for(start_gate)
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        result_path.write_text(
            json.dumps(_payload(lock, acquired=False, busy=True), sort_keys=True),
            encoding="utf-8",
        )
        return 23

    result_path.write_text(
        json.dumps(_payload(lock, acquired=True, busy=False), sort_keys=True),
        encoding="utf-8",
    )
    try:
        _wait_for(release_gate)
        return 0
    finally:
        lock.release()


def fork_prepare_interrupt(child_marker: Path) -> int:
    """Inject ambiguous guard acquisition inside the registered before-fork hook."""

    if not hasattr(os, "fork"):
        return 91

    class _AcquireThenInterruptGuard:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def acquire(self) -> bool:
            # Deliberately acquire first, then raise. Retrying a non-reentrant
            # guard would deadlock; allowing the callback exception to escape can
            # let CPython continue the fork. Production must fail-stop instead.
            self._lock.acquire()
            raise KeyboardInterrupt

        def release(self) -> None:
            self._lock.release()

        def __enter__(self) -> "_AcquireThenInterruptGuard":
            self._lock.acquire()
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            self._lock.release()

    AuthorityOwnerLock._fork_guard = _AcquireThenInterruptGuard()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - must be unreachable with the fixed hook
        child_marker.write_text("child-ran", encoding="utf-8")
        os._exit(99)

    child_marker.write_text(f"parent-returned:{pid}", encoding="utf-8")
    return 98


def fork_parent_release_interrupt(parent_marker: Path) -> int:
    """Inject an interruption after the parent at-fork guard release succeeds."""

    if not hasattr(os, "fork"):
        return 91

    class _ReleaseThenInterruptGuard:
        def __init__(self) -> None:
            self._lock = threading.Lock()

        def acquire(self) -> bool:
            return self._lock.acquire()

        def release(self) -> None:
            self._lock.release()
            raise KeyboardInterrupt

        def __enter__(self) -> "_ReleaseThenInterruptGuard":
            self._lock.acquire()
            return self

        def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
            self._lock.release()

    AuthorityOwnerLock._fork_guard = _ReleaseThenInterruptGuard()
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child should exit normally and immediately
        os._exit(0)

    parent_marker.write_text(f"parent-returned:{pid}", encoding="utf-8")
    return 98


def hidden_open_interrupt(state_dir: Path) -> int:
    """Create a real fd, then raise before AuthorityOwnerLock can publish its integer."""

    owner = AuthorityOwnerLock(state_dir)
    original_open = os.open

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
            # The raw integer is intentionally not returned to the caller. The
            # production owner must terminate so process death closes this hidden
            # descriptor; normal Python cleanup cannot name it safely.
            raise KeyboardInterrupt
        return fd

    os.open = open_then_interrupt
    owner.acquire()
    return 97


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "probe":
        return probe(Path(argv[2]))
    if len(argv) == 6 and argv[1] == "race":
        return race(Path(argv[2]), Path(argv[3]), Path(argv[4]), Path(argv[5]))
    if len(argv) == 3 and argv[1] == "fork-prepare-interrupt":
        return fork_prepare_interrupt(Path(argv[2]))
    if len(argv) == 3 and argv[1] == "fork-parent-release-interrupt":
        return fork_parent_release_interrupt(Path(argv[2]))
    if len(argv) == 3 and argv[1] == "hidden-open-interrupt":
        return hidden_open_interrupt(Path(argv[2]))
    raise SystemExit(
        "usage: _m7_authority_worker.py probe STATE_DIR | "
        "race STATE_DIR START_GATE RELEASE_GATE RESULT_PATH | "
        "fork-prepare-interrupt CHILD_MARKER | "
        "fork-parent-release-interrupt PARENT_MARKER | "
        "hidden-open-interrupt STATE_DIR"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))