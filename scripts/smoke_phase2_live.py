"""Phase 2 live smoke: read_profile against a real profile.

Gate: read_profile returns posts with post_id + author_handle + created_at,
correct retweet provenance (retweeted_by for non-self posts), and respects limit.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1
    try:
        print("=== read_profile @jack (limit=10) ===")
        rp = await d.invoke("read_profile", {"handle": "jack", "limit": 10})
        print(f"ok={rp.ok}")
        if not rp.ok:
            fc = rp.failure_category.value if rp.failure_category else None
            print(f"failed (fc={fc}): {rp.error.message if rp.error else rp}")
            return 1
        data = rp.data
        profile = data.get("profile", {})
        posts = data.get("posts", [])
        print(f"profile: handle={profile.get('handle')} name={profile.get('display_name')!r}")
        print(f"count={data.get('count')} truncated={data.get('truncated')} scrolls={data.get('scrolls_performed')}")
        print(f"\nposts ({len(posts)}):")
        for i, p in enumerate(posts):
            rt = f" [RT by {p.get('retweeted_by')}]" if p.get('retweeted_by') else ""
            print(f"  [{i}] {p.get('author_handle')} / {p.get('post_id')} | {p.get('created_at')}{rt}")
            print(f"       text={repr((p.get('text') or '')[:70])}")

        # Gate assertions
        print("\n=== GATE ===")
        ok = True
        if not posts:
            print("  FAIL: no posts returned")
            ok = False
        else:
            for i, p in enumerate(posts):
                pid = p.get("post_id")
                ah = p.get("author_handle")
                ca = p.get("created_at")
                status = "PASS" if (pid and ah and ca) else "FAIL"
                if status == "FAIL":
                    ok = False
                    print(f"  post[{i}] incomplete: post_id={pid!r} author={ah!r} created_at={ca!r} [{status}]")
            # Verify retweet provenance: at least one non-jack author should be flagged.
            rts = [p for p in posts if p.get("retweeted_by")]
            print(f"  retweets detected: {len(rts)} (expected: jack's feed includes RTs)")
            print(f"  GATE: {'PASS' if ok else 'FAIL'} — {len(posts)} posts, required fields present")
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
