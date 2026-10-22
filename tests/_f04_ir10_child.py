"""F04-IR10 child: the deferred-cleanup lifecycle under abandonment.

Parks an accept lease at a seam that never enters ConnectNamedPipe
(cancellation ineffective by construction), drives close() past its
total drain bound (abandonment recorded, handle left to the holder),
then RELEASES the parked lease — the holder's exit must perform the
deferred cleanup exactly once — and finally proves a SUCCESSOR
endpoint can bind the same pipe name (the retained-handle consequence
your review identified). A second phase covers the never-returns
containment: a lease parked permanently keeps the handle until process
exit (documented limit — no close under a live operation).
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

    resume.set()
    # The parked lease now proceeds INTO ConnectNamedPipe on the
    # still-open handle; a client connect makes the kernel call return
    # so the lease holder can exit and perform the deferred cleanup.
    from webwire.authority_ipc_endpoint import _WindowsPipeConnection

    unblocker = _WindowsPipeConnection.open_client(ep.path)
    unblocker.close()
    th.join(timeout=15)
    time.sleep(0.3)

    record = {
        "close_elapsed_s": round(elapsed, 2),
        "abandoned_at_close": abandoned_at_close,
        "handle_owned_at_close": handle_owned_at_close,
        "deferred_handle_after_release": ep._handle,
        "accept_returned": result,
        "lifecycle": ep._lifecycle,
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
