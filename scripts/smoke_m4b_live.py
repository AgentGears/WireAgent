"""Live M4b verification — reply_multi_image against the fixture post.

The milestone close-out ritual: phase-1 (preview+token, actor check) →
phase-2 (real reply with the two fixture images on the fixture post,
text marked as test) → thread read-back → journal write-facts inspection.
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

TARGET = "https://x.com/infaag/status/2102451358305771541"
IMAGES = [".webwire/test-media/test_red.png", ".webwire/test-media/test_blue.png"]
TEXT = "Agent-WebWire M4b reply_multi_image test — two images."


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
    show("start", started)
    if not started.ok:
        return 1
    try:
        who = await d.invoke("whoami")
        show("whoami (binds actor)", who)
        if not who.ok:
            print("\nNot authenticated — aborting before any write.", flush=True)
            return 1

        p1 = await d.invoke("reply_multi_image",
                            {"post_url": TARGET, "text": TEXT, "image_paths": IMAGES})
        show("reply_multi_image phase-1 (confirmation_required)", p1)
        tok = None
        try:
            tok = p1.data["data"]["confirmation_token"]
        except Exception:
            pass
        if not tok:
            print("\nNo token — aborting before submit.", flush=True)
            return 1

        p2 = await d.invoke("reply_multi_image",
                            {"post_url": TARGET, "text": TEXT, "image_paths": IMAGES,
                             "confirmation_token": tok})
        show("reply_multi_image phase-2 (submit + verify)", p2)
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
