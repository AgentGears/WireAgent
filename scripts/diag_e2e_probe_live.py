"""Diagnostic for the 2026-09-22 live E2E: is the read failure the specific
post (deleted?) or the read path (DOM drift)?

Probes, each using a DIFFERENT DOM path than read's article selector:
  1. read_profile @infaag (enumerate profile posts — data-testid cellInnerDiv)
  2. read_search "from:infaag" (search results — a third DOM surface)
  3. raw observe of the failing post URL — capture visible text (deleted-post
     banner vs empty timeline vs login wall)
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

TARGET = "https://x.com/infaag/status/2075426870883954690"


def show(label: str, r) -> None:
    fc = getattr(r, "failure_category", None)
    err = getattr(r, "error", None)
    print(f"\n=== {label}: ok={getattr(r, 'ok', None)} failure_category={fc}", flush=True)
    if err is not None:
        print(f"    error: {err.message}", flush=True)
    s = json.dumps(getattr(r, "data", None), default=str, ensure_ascii=False)
    print("    data: " + (s[:2000] + (" …" if len(s) > 2000 else "")), flush=True)


async def main() -> int:
    d = Dispatcher()
    started = await d.start()
    if not started.ok:
        show("start", started)
        return 1
    try:
        show("whoami", await d.invoke("whoami"))
        show("read_profile @infaag", await d.invoke(
            "read_profile", {"handle": "infaag", "limit": 10}))
        show("read_search from:infaag", await d.invoke(
            "read_search", {"query": "from:infaag", "tab": "latest", "limit": 5}))

        # Raw page text at the failing URL via the read-only broker.
        broker = d._broker
        nav = await broker.navigate(TARGET)
        print(f"\n=== raw navigate to TARGET: ok={nav.ok}", flush=True)
        obs = await broker.observe()
        s = json.dumps(getattr(obs, "data", None), default=str, ensure_ascii=False)
        print("    observe: " + s[:2000] + (" …" if len(s) > 2000 else ""), flush=True)
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
