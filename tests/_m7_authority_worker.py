"""Fresh-process probes for M7 Layer-1 AuthorityOwnerLock tests."""

from __future__ import annotations

import json
import sys
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


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[1] == "probe":
        return probe(Path(argv[2]))
    if len(argv) == 6 and argv[1] == "race":
        return race(Path(argv[2]), Path(argv[3]), Path(argv[4]), Path(argv[5]))
    raise SystemExit(
        "usage: _m7_authority_worker.py probe STATE_DIR | "
        "race STATE_DIR START_GATE RELEASE_GATE RESULT_PATH"
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
