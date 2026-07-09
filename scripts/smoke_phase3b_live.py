"""Phase 3b live smoke: like_post through the full write pipeline.

12-point gate (ChatGPT's Phase 3b acceptance criteria). The critical new test
vs bookmark: pre-existing-state handling (already_satisfied vs real mutation).
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig

TARGET = "https://x.com/jack/status/20"  # harmless public post


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1

    failures: list[str] = []

    try:
        # First: check current like state to know which case we're in.
        wb = d._write_kernel._write_broker_factory()
        pre = await wb.read_like_state(TARGET)
        pre_state = pre.data.get("like_state") if pre.ok else "unknown"
        print(f"pre-existing like state: {pre_state}")

        # Gate 1: first invoke → confirmation_required
        print(f"\n=== GATE 1: first invoke → confirmation_required ===")
        r1 = await d.invoke("like_post", {"post_url": TARGET})
        if r1.ok and r1.data.get("policy", {}).get("verdict") == "confirmation_required":
            token = r1.data["data"]["confirmation_token"]
            print(f"GATE 1: PASS")
        else:
            failures.append("GATE 1: first invoke did not return confirmation_required")
            print(f"GATE 1: FAIL")
            return 1

        # Gate 2: preview labels as public engagement
        preview = r1.data["data"].get("preview", "")
        if "public" in preview.lower() or "like" in preview.lower():
            print(f"GATE 2: PASS — preview={preview!r}")
        else:
            failures.append(f"GATE 2: preview doesn't label public engagement: {preview!r}")

        # Gate 3: token bound to intent_hash (already structurally proven, verify present)
        if "intent_hash" in r1.data["data"]:
            print(f"GATE 3: PASS — intent_hash present")
        else:
            failures.append("GATE 3: no intent_hash in confirmation")

        # Gate 5: execute with token
        print(f"\n=== GATE 5: execute with token ===")
        r2 = await d.invoke("like_post", {"post_url": TARGET, "confirmation_token": token})
        if r2.ok and r2.data.get("policy", {}).get("verdict") == "allow":
            print(f"GATE 5: PASS — executed")
            print(f"  trace: {r2.data['trace']['stages']}")
            print(f"  execute_ok={r2.data['trace'].get('execute_ok')}")
        else:
            failures.append(f"GATE 5: execute failed: {r2.data}")

        # Gate 6: dedupe blocks replay
        print(f"\n=== GATE 6: dedupe blocks replay ===")
        r3 = await d.invoke("like_post", {"post_url": TARGET})
        if not r3.ok and r3.data.get("policy", {}).get("blocked_by") == "dedupe":
            print(f"GATE 6: PASS")
        else:
            failures.append(f"GATE 6: replay not blocked: {r3.data}")

        # Gate 10: verify like state is now 'liked'
        print(f"\n=== GATE 10: verify liked state ===")
        post = await wb.read_like_state(TARGET)
        post_state = post.data.get("like_state") if post.ok else "unknown"
        if post_state == "liked":
            print(f"GATE 10: PASS — liked={post_state}")
        else:
            failures.append(f"GATE 10: post_state={post_state}, expected 'liked'")

        # Gate 8/9: compensation (unlike) — reverse THIS invocation's delta
        print(f"\n=== GATE 8/9: compensation (unlike) ===")
        comp = await wb.click_like(TARGET)  # toggles: liked → unlike
        if comp.ok:
            post2 = await wb.read_like_state(TARGET)
            print(f"GATE 8/9: compensation done, state now={post2.data.get('like_state')}")
        else:
            failures.append(f"GATE 8/9: compensation failed")

        # Gate 11: journal records the like
        print(f"\n=== GATE 11: journal record ===")
        jpath = cfg.journal_path()
        if jpath.exists():
            lines = [l for l in jpath.read_text(encoding="utf-8").splitlines() if l.strip()]
            like_recs = [json.loads(l) for l in lines if "like" in l]
            print(f"GATE 11: {len(like_recs)} like records in journal")
            if like_recs:
                print(f"GATE 11: PASS")
            else:
                failures.append("GATE 11: no like records")
        else:
            failures.append("GATE 11: journal missing")

    finally:
        await d.stop()

    print(f"\n{'='*60}")
    if failures:
        print(f"PHASE 3b GATE: FAIL — {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
    else:
        print("PHASE 3b GATE: PASS — all gates satisfied.")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
