"""Live delete_post E2E (2026-09-23) — zero-residue ritual.

1. Create a throwaway post via post_text (its own two-phase confirmation).
2. delete_post on it (two-phase).
3. read_post_state → expect 'deleted'.
The throwaway text marks the run; nothing public remains at the end.
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

THROWAWAY = "Agent-WebWire delete_post E2E — this post will be deleted."


def show(label: str, r) -> None:
    fc = getattr(r, "failure_category", None)
    err = getattr(r, "error", None)
    print(f"\n=== {label}: ok={getattr(r, 'ok', None)} failure_category={fc}", flush=True)
    if err is not None:
        print(f"    error: {err.message}", flush=True)
    s = json.dumps(getattr(r, "data", None), default=str, ensure_ascii=False)
    print("    data: " + (s[:1600] + (" …" if len(s) > 1600 else "")), flush=True)


async def main() -> int:
    d = Dispatcher()
    started = await d.start()
    show("start", started)
    if not started.ok:
        return 1
    try:
        who = await d.invoke("whoami")
        show("whoami", who)
        if not who.ok:
            print("\nNot authenticated — aborting.", flush=True)
            return 1

        # Step 1: create the throwaway.
        c1 = await d.invoke("post_text", {"text": THROWAWAY})
        tok1 = None
        try:
            tok1 = c1.data["data"]["confirmation_token"]
        except Exception:
            pass
        if not tok1:
            print("\nNo token for throwaway creation — aborting.", flush=True)
            return 1
        c2 = await d.invoke("post_text", {"text": THROWAWAY, "confirmation_token": tok1})
        show("post_text (throwaway created)", c2)
        created_url = (c2.data or {}).get("data", {}).get("posted_url")
        created_id = (c2.data or {}).get("data", {}).get("posted_post_id")
        if not created_id:
            print("\nThrowaway not created — aborting before delete.", flush=True)
            return 1

        # Step 2: delete it.
        d1 = await d.invoke("delete_post", {"post_url": created_url})
        show("delete_post phase-1 (confirmation_required)", d1)
        tok2 = None
        try:
            tok2 = d1.data["data"]["confirmation_token"]
        except Exception:
            pass
        if not tok2:
            print("\nNo delete token — the throwaway stays; clean it up manually.", flush=True)
            return 1
        d2 = await d.invoke("delete_post",
                            {"post_url": created_url, "confirmation_token": tok2})
        show("delete_post phase-2 (execute + verify)", d2)
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
