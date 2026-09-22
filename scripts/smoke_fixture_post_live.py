"""Live fixture post via post_text (P1, 2026-09-22).

Replaces the deleted M1 fixture post as the test target for read/bookmark/
reply/quote capabilities — and doubles as the first live run of all four P0
kernel fixes committed today (e5d2081):
  - registry gate: post_text must clear it (action "post" is registered)
  - actor binding: whoami first → dedupe key must start "infaag|"
  - write facts: the journal record must carry capability_tier/action_type/
    risk_tier/dedupe_key (the fields both safety stores hydrate from)
  - verification: posted_and_verified via inline post-submit verify

Chain: start -> whoami -> post_text phase-1 (preview+token) -> phase-2
(submit) -> read the new post URL -> stop. PUBLIC IRREVERSIBLE write,
explicitly requested; text is marked as a fixture.
"""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

FIXTURE_TEXT = (
    "Agent-WebWire v0.2 fixture post — P0 safety-fix live check, 2026-09-22. "
    "(Test target for read/bookmark/reply/quote capabilities.)"
)


def show(label: str, r) -> None:
    fc = getattr(r, "failure_category", None)
    err = getattr(r, "error", None)
    print(f"\n=== {label}: ok={getattr(r, 'ok', None)} failure_category={fc}", flush=True)
    if err is not None:
        print(f"    error: {err.message}", flush=True)
    s = json.dumps(getattr(r, "data", None), default=str, ensure_ascii=False)
    print("    data: " + (s[:1800] + (" …" if len(s) > 1800 else "")), flush=True)


async def main() -> int:
    d = Dispatcher()
    started = await d.start()
    show("start", started)
    if not started.ok:
        return 1
    try:
        who = await d.invoke("whoami")
        show("whoami (binds actor identity)", who)
        if not who.ok:
            print("\nwhoami failed — not authenticated; aborting before any write.", flush=True)
            return 1

        # Phase 1: preview + confirmation token.
        p1 = await d.invoke("post_text", {"text": FIXTURE_TEXT})
        show("post_text phase-1 (confirmation_required)", p1)
        tok = None
        try:
            tok = p1.data["data"]["confirmation_token"]
        except Exception:
            pass
        if not tok:
            print("\nNo token — aborting before submit.", flush=True)
            return 1

        # Phase 2: submit the approved intent.
        p2 = await d.invoke("post_text", {"text": FIXTURE_TEXT, "confirmation_token": tok})
        show("post_text phase-2 (submit + inline verify)", p2)
        data = (p2.data or {}).get("data", {}) or {}
        posted_url = data.get("posted_url")

        # Read back the new post — re-tests the read path on a fresh target.
        if posted_url:
            rd = await d.invoke("read", {"post_url": posted_url})
            show(f"read-back {posted_url}", rd)
    finally:
        show("stop", await d.stop())
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
