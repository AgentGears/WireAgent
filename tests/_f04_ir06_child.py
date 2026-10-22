"""F04-IR06 child: a deliberately uncancellable accept lease.

The seam parks BEFORE ConnectNamedPipe and never proceeds — the lease
is held and no kernel cancellation can release it (cancellation
ineffective BY CONSTRUCTION, without needing to force a real
CancelIoEx failure). The child then calls close() and reports whether
the overall bound held, whether abandonment was recorded, and whether
the handle remained owned (never closed under the lease).
"""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint  # noqa: E402


def main() -> int:
    ep = WindowsNamedPipeEndpoint(Path(sys.argv[1]))
    ep.bind()
    stuck = threading.Event()
    release = threading.Event()

    def _stuck_seam(handle: object) -> None:
        stuck.set()
        release.wait(timeout=60)  # never enters ConnectNamedPipe

    ep._before_accept_attempt = _stuck_seam  # type: ignore[method-assign]

    def one_accept() -> None:
        ep.accept()

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    assert stuck.wait(timeout=10), "lease never parked"
    t0 = time.monotonic()
    ep.close()  # must be bounded and observable
    elapsed = time.monotonic() - t0
    print(
        json.dumps(
            {
                "elapsed_s": round(elapsed, 2),
                "abandoned": getattr(ep, "_close_abandoned", False),
                "handle_still_owned": ep._handle is not None,
                "lifecycle": ep._lifecycle,
                "pending": ep._pending_accepts,
            }
        )
    )
    release.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
