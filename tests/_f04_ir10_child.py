"""F04-IR10 child: the deferred-cleanup lifecycle under abandonment.

Parks an accept lease at a seam that never enters ConnectNamedPipe
(cancellation ineffective by construction), drives close() past its
total drain bound (abandonment recorded, handle left to the holder),
then RELEASES the parked lease with NO client — under the IR11/IR13
retirement protocol the lease retires client-free, the deferred
cleanup runs, and a SUCCESSOR endpoint binds the same pipe name (the
retained-handle consequence the review identified). A kernel
operation already executing when cancellation is requested is covered
by the nominal blocked-accept shutdown test (cancellation effective)
and by the documented process-exit containment (never returns).
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

    t0 = time.monotonic()
    ep.close()
    elapsed = time.monotonic() - t0
    abandoned_at_close = getattr(ep, "_close_abandoned", False)
    handle_owned_at_close = ep._handle is not None

    # HONEST CLAIM (post-IR13 reconciliation): a parked-then-released
    # lease now ALWAYS retires client-free at the operation-entry
    # re-check — the kernel-entry attempt that this child originally
    # unblocked with a client is exactly the race IR13 eliminated. A
    # genuinely kernel-completed abandonment cannot be forced
    # deterministically (it requires cancellation to fail against an
    # ENTERED synchronous call), and the cancellation-effective path is
    # proven by the nominal blocked-accept shutdown test. This child
    # therefore proves the outcome contract: abandonment -> retirement
    # -> full resource reconciliation -> successor binds the same name.
    resume.set()
    th.join(timeout=15)
    time.sleep(0.3)

    record = {
        "close_elapsed_s": round(elapsed, 2),
        "abandoned_at_close": abandoned_at_close,
        "handle_owned_at_close": handle_owned_at_close,
        "deferred_handle_after_release": ep._handle,
        "accept_returned": result,
        "lifecycle": ep._lifecycle,
        # Full cleanup reconciliation (review evidence table).
        "pending_after": ep._pending_accepts,
        "acl_after": ep._acl,
        "sd_buffer_after": ep._sd_buffer,
        "sa_after": ep._sa,
        "close_abandoned_reconciled": getattr(ep, "_close_abandoned", None),
    }

    # The consequence that matters: a SUCCESSOR binds the same name.
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
