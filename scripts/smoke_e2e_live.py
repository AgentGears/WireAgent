"""Live E2E smoke — read chain + two-phase bookmark canary (2026-09-22).

Chain under test:
  start (owned browser, session restore) -> whoami (auth gate) -> health
  -> read (golden read) -> bookmark_post phase-1 (confirmation_required)
  -> bookmark_post phase-2 (execute + verify) -> stop.

PUBLIC-CONTENT WRITES ARE DELIBERATELY OUT OF SCOPE. Bookmark is the
PRIVATE_REVERSIBLE canary tier; no post/reply/quote/like is invoked.
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

# Documented test fixture post (STATE.md): @infaag's M1 photo post.
TARGET = "https://x.com/infaag/status/2075426870883954690"


def show(label: str, r) -> None:
    ok = getattr(r, "ok", None)
    fc = getattr(r, "failure_category", None)
    err = getattr(r, "error", None)
    print(f"\n=== {label}: ok={ok} failure_category={fc}", flush=True)
    if err is not None:
        print(f"    error: {err.message}", flush=True)
    data = getattr(r, "data", None)
    s = json.dumps(data, default=str, ensure_ascii=False)
    print("    data: " + (s[:1500] + (" …" if len(s) > 1500 else "")), flush=True)


async def main() -> int:
    d = Dispatcher()
    started = await d.start()
    show("start (owned browser + session restore)", started)
    if not started.ok:
        return 1
    try:
        show("whoami (auth gate)", await d.invoke("whoami"))
        show("health", await d.invoke("health"))
        show(f"read {TARGET}", await d.invoke("read", {"post_url": TARGET}))

        # Two-phase bookmark: the WriteKernel confirmation-token flow.
        p1 = await d.invoke("bookmark_post", {"post_url": TARGET})
        show("bookmark phase-1 (expect confirmation_required)", p1)
        tok = None
        try:
            tok = p1.data["data"]["confirmation_token"]
        except Exception:
            try:
                tok = p1.data["policy"]["confirmation_token"]["token"]
            except Exception:
                tok = None
        if not tok:
            print("\nNo confirmation token returned — aborting write phase.", flush=True)
        else:
            p2 = await d.invoke(
                "bookmark_post", {"post_url": TARGET, "confirmation_token": tok}
            )
            show("bookmark phase-2 (expect executed + verified)", p2)
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
