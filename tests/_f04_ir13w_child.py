"""F04-IR13 residual-window child: shutdown abandons in the exact
check-to-kernel-call interval, with NO client at release.

Parks the listener at the _before_kernel_call seam — AFTER the final
lifecycle re-check has released the lock, IMMEDIATELY BEFORE the
synchronous ConnectNamedPipe. close() runs to abandonment while parked
there (the epoch was already stamped; the post-barrier re-check already
passed). The park then releases with NO client: the weakened synchronous
guarantee's containment behavior is characterized honestly — the listener
enters the blocking call (no subsequent cancellation request exists),
the lease stays outstanding, the handle stays owned by the lease holder
(never closed under a live call), close() has already returned bounded.
A client connection then demonstrates the EVENT-DRIVEN retirement: the
kernel call returns, the post-return critical section observes CLOSING,
the deferred cleanup fires, and a successor binds the same name.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire.authority_ipc_endpoint import (  # noqa: E402
    WindowsNamedPipeEndpoint,
    _WindowsPipeConnection,
)


def main() -> int:
    scratch = Path(sys.argv[1])
    ep = WindowsNamedPipeEndpoint(scratch)
    ep.bind()
    park = threading.Event()
    resume = threading.Event()

    def _window_seam(handle: object) -> None:
        park.set()
        assert resume.wait(timeout=60), "never resumed"

    ep._before_kernel_call = _window_seam  # type: ignore[method-assign]
    result: list = []

    def one_accept() -> None:
        result.append(ep.accept())

    th = threading.Thread(target=one_accept, daemon=True)
    th.start()
    assert park.wait(timeout=10)

    ep.close()  # runs to abandonment WHILE parked in the residual window
    containment = {
        "abandoned_at_close": getattr(ep, "_close_abandoned", False),
        "close_returned_bounded": True,
        # The containment state: lease outstanding, handle owned by the
        # lease holder, never closed under the (about-to-start) call.
        "pending_during_window": ep._pending_accepts,
        "handle_owned_during_window": ep._handle is not None,
        "lifecycle": ep._lifecycle,
    }

    resume.set()  # NO client: the listener enters the blocking call now

    # Give the listener a moment to enter the syscall, then characterize:
    # it is blocked (no client) with the lease outstanding — containment.
    import time

    time.sleep(1.0)
    containment["listener_blocked_no_client"] = th.is_alive()
    containment["pending_while_blocked"] = ep._pending_accepts

    # Event-driven retirement: a client makes the kernel call RETURN.
    unblocker = _WindowsPipeConnection.open_client(ep.path)
    unblocker.close()
    th.join(timeout=15)
    time.sleep(0.3)

    record = {
        **containment,
        "accept_returned": result,
        "pending_after": ep._pending_accepts,
        "handle_after": ep._handle,
        "acl_after": ep._acl,
        "sd_buffer_after": ep._sd_buffer,
        "sa_after": ep._sa,
        "close_abandoned_reconciled": getattr(ep, "_close_abandoned", None),
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
