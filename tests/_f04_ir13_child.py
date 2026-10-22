"""F04-IR13 child: the post-permission boundary — shutdown wins between
the final lifecycle check and kernel entry, with NO client.

Parks the listener at the _before_kernel_entry seam (AFTER the entry
epoch is stamped under the lock, AFTER the permission check, BEFORE the
post-barrier re-check and the kernel call). close() runs to abandonment
while the listener is parked exactly at the boundary the review names.
The park releases with NO client connection: the post-barrier re-check
must observe the stale epoch / CLOSING and retire — lease zero, handle
and security resources released, successor binds the same name.
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

    def _boundary_seam(handle: object) -> None:
        park.set()
        assert resume.wait(timeout=60), "never resumed"

    ep._before_kernel_entry = _boundary_seam  # type: ignore[method-assign]
    result: list = []

    def one_accept() -> None:
        result.append(ep.accept())

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    assert park.wait(timeout=10)

    ep.close()  # to abandonment, while parked at the boundary
    abandoned = getattr(ep, "_close_abandoned", False)

    resume.set()  # NO client — the post-barrier re-check must retire it
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
