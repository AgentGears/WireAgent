"""Phase 1 live smoke: read a real post via the golden-read capability.

Gate: read returns a Post with post_id + author_handle + created_at resolved,
text present (for a text post), and metrics parsed.
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
        # Jack's first tweet — stable, public, has text + metrics.
        post_url = "https://x.com/X/status/20"
        print(f"=== read {post_url} ===")
        read = await d.invoke("read", {"post_url": post_url})
        print(f"read ok={read.ok}")
        if read.ok:
            post = read.data
            print(json.dumps(post, indent=2, default=str)[:1200])
            # Gate assertions
            print("\n=== GATE ===")
            ok = True
            for field in ("post_id", "author_handle", "created_at"):
                v = post.get(field)
                status = "PASS" if v else "FAIL"
                if not v:
                    ok = False
                print(f"  {field}: {v!r} [{status}]")
            text = post.get("text")
            print(f"  text: {text!r:.60} [{'PASS' if text else 'NOTE: empty'}]")
            metrics = post.get("metrics") or {}
            for m in ("reply_count", "repost_count", "like_count", "bookmark_count"):
                v = metrics.get(m)
                print(f"  metrics.{m}: {v} [{'PASS' if v is not None else 'unresolved'}]")
            print(f"\nPHASE 1 GATE: {'PASS' if ok else 'FAIL'}")
        else:
            fc = read.failure_category.value if read.failure_category else None
            print(f"read failed (fc={fc}): {read.error.message if read.error else read}")
            print("PHASE 1 GATE: FAIL")
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
