"""F04-IR11 child: a pre-kernel lease released after shutdown — with NO
new client — must retire without ever entering ConnectNamedPipe.

Parks an accept lease before the kernel call, drives close() past its
drain bound (abandonment), then releases the park. The listener must
re-check the lifecycle at the operation-entry transition: it releases
the lease, performs the deferred cleanup, and returns None — WITHOUT
starting a blocking kernel operation and without any client connection.
The child then verifies the full cleanup reconciliation and that a
successor binds the same pipe name.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire.authority_ipc_endpoint import WindowsNamedPipeEndpoint  # noqa: E402


def main() -> int:
    scratch = Path(sys.argv[1])
    ep = WindowsNamedPipeEndpoint(scratch)
    ep.bind()
    park = threading.Event()
    resume = threading.Event()

    def _parked_seam(handle: object) -> None:
        park.set()
        assert resume.wait(timeout=60), "never resumed"

    ep._before_accept_attempt = _parked_seam  # type: ignore[method-assign]
    result: list = []

    def one_accept() -> None:
        result.append(ep.accept())

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    assert park.wait(timeout=10)

    ep.close()  # runs to abandonment (cancellation cannot reach a parked seam)
    abandoned = getattr(ep, "_close_abandoned", False)

    resume.set()  # NO client connection — the release alone must retire it
    th.join(timeout=15)

    record = {
        "abandoned_at_close": abandoned,
        "accept_returned": result,
        "pending_after": ep._pending_accepts,
        "handle_after": ep._handle,
        "acl_after": ep._acl,
        "sd_buffer_after": ep._sd_buffer,
        "sa_after": ep._sa,
        "close_abandoned_reconciled": getattr(ep, "_close_abandoned", None),
        "lifecycle": ep._lifecycle,
    }

    successor_bound = False
    try:
        successor = WindowsNamedPipeEndpoint(scratch)
        successor.bind()
        successor_bound = True
        successor.close()
    except RuntimeError as exc:
        record["successor_bind_error"] = repr(exc)
    record["successor_bound_same_name"] = successor_bound

    print(json.dumps(record, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
