"""Phase 2b live smoke: read_thread against a real thread page.

Gate: read_thread returns the target post (matched by post_id), replies with
correct relationships, and honest coverage semantics.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig

TARGET = "https://x.com/jack/status/20"  # root post, 17K+ replies


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed")
        return 1

    failures: list[str] = []

    try:
        print(f"=== read_thread {TARGET} ===")
        rt = await d.invoke("read_thread", {"post_url": TARGET, "limit": 15})
        print(f"ok={rt.ok}")
        if not rt.ok:
            fc = rt.failure_category.value if rt.failure_category else None
            print(f"failed (fc={fc}): {rt.error.message if rt.error else rt}")
            return 1

        data = rt.data
        target = data.get("target_post", {})
        ancestors = data.get("ancestors", [])
        replies = data.get("replies", [])
        coverage = data.get("coverage", {})

        # Gate 1: target post_id matches URL
        print(f"\ntarget: post_id={target.get('post_id')} handle={target.get('author_handle')}")
        if str(target.get("post_id")) == "20":
            print("GATE 1: PASS — target post_id=20 matches URL")
        else:
            failures.append(f"GATE 1: target post_id={target.get('post_id')}, expected 20")

        # Gate 2: target relationship = "target"
        if target.get("relationship") == "target":
            print("GATE 2: PASS — relationship=target")
        else:
            failures.append(f"GATE 2: relationship={target.get('relationship')}, expected 'target'")

        # Gate 3: ancestors empty (root post)
        if len(ancestors) == 0:
            print("GATE 3: PASS — 0 ancestors (root post)")
        else:
            print(f"GATE 3: NOTE — {len(ancestors)} ancestors (unexpected for root post)")

        # Gate 4: replies populated
        print(f"\nreplies ({len(replies)}):")
        for i, rep in enumerate(replies[:5]):
            print(f"  [{i}] {rep.get('author_handle')} / {rep.get('post_id')} rel={rep.get('relationship')}")
            print(f"       text={repr((rep.get('text') or '')[:50])}")
        if len(replies) > 0:
            print(f"\nGATE 4: PASS — {len(replies)} replies")
        else:
            failures.append("GATE 4: no replies returned")

        # Gate 5: coverage semantics honest
        print(f"\ncoverage: {json.dumps(coverage, indent=2)}")
        if coverage.get("complete") is False:
            print("GATE 5a: PASS — complete=False")
        else:
            failures.append("GATE 5a: complete should be False")
        if coverage.get("replies_total") is None:
            print("GATE 5b: PASS — replies_total=None (not a crawl bound)")
        else:
            failures.append("GATE 5b: replies_total should be None")
        if coverage.get("mode") == "visible_thread_slice":
            print("GATE 5c: PASS — mode=visible_thread_slice")
        else:
            failures.append(f"GATE 5c: mode={coverage.get('mode')}")

        # Gate 6: reply relationships = "reply"
        all_reply = all(r.get("relationship") == "reply" for r in replies)
        if all_reply and replies:
            print("GATE 6: PASS — all replies have relationship='reply'")
        else:
            failures.append("GATE 6: not all replies have relationship='reply'")

    finally:
        await d.stop()

    print(f"\n{'='*60}")
    if failures:
        print(f"PHASE 2b GATE: FAIL — {len(failures)} failure(s):")
        for f in failures:
            print(f"  - {f}")
    else:
        print("PHASE 2b GATE: PASS — all gates satisfied.")
    print("=" * 60)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
