"""Fresh-process probe for M7 Layer-1 AuthorityOwnerLock tests."""

from __future__ import annotations

import json
import sys
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


def probe(state_dir: Path) -> int:
    lock = AuthorityOwnerLock(state_dir)
    try:
        lock.acquire()
    except AuthorityBusyError:
        _emit(
            {
                "acquired": False,
                "busy": True,
                "domain": str(lock.authority_domain),
                "lock_path": str(lock.lock_path),
            }
        )
        return 23

    try:
        _emit(
            {
                "acquired": True,
                "busy": False,
                "domain": str(lock.authority_domain),
                "lock_path": str(lock.lock_path),
            }
        )
        return 0
    finally:
        lock.release()


def main(argv: list[str]) -> int:
    if len(argv) != 3 or argv[1] != "probe":
        raise SystemExit("usage: _m7_authority_worker.py probe STATE_DIR")
    return probe(Path(argv[2]))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
