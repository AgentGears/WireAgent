"""Control run for the 2026-09-22 E2E diagnosis: same capabilities, subject
that certainly has posts. If these return data, the read path is healthy and
the @infaag zero-post / missing-post results mean the account's posts (incl.
the M1 fixture) were deleted since July.
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher


def show(label: str, r) -> None:
    fc = getattr(r, "failure_category", None)
    err = getattr(r, "error", None)
    print(f"\n=== {label}: ok={getattr(r, 'ok', None)} failure_category={fc}", flush=True)
    if err is not None:
        print(f"    error: {err.message}", flush=True)
    data = getattr(r, "data", None) or {}
    # Compact: for profile/search show only counts + first post ids.
    if isinstance(data, dict) and "posts" in data:
        posts = data.get("posts") or []
        print(f"    count={data.get('count')} first_ids={[p.get('post_id') for p in posts[:3]]}", flush=True)
    elif isinstance(data, dict) and "results" in data:
        res = data.get("results") or []
        print(f"    count={data.get('count')} first={[r2.get('post_id') for r2 in res[:3]]}", flush=True)
    else:
        s = json.dumps(data, default=str, ensure_ascii=False)
        print("    data: " + (s[:800] + (" …" if len(s) > 800 else "")), flush=True)


async def main() -> int:
    d = Dispatcher()
    started = await d.start()
    if not started.ok:
        show("start", started)
        return 1
    try:
        show("read_profile @elonmusk (control)", await d.invoke(
            "read_profile", {"handle": "elonmusk", "limit": 5}))
        show("read_search 'python' top (control)", await d.invoke(
            "read_search", {"query": "python", "tab": "top", "limit": 3}))
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
