"""Diagnostic: probe a real X post URL to see the DOM shape for Phase 1 golden read.

Reads a post via the broker, dumps observe() targets + probes key data-testid
elements via query_attr, so we know whether the golden read is AX-based or
needs query_attr/extract.
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
        broker = d._broker
        assert broker is not None

        # A stable, public post. Using X's own announcement-style URL.
        # (Any public post works for DOM probing.)
        post_url = "https://x.com/X/status/20"
        print(f"=== navigate {post_url} ===")
        nav = await broker.navigate(post_url)
        print(f"navigate ok={nav.ok}")
        if not nav.ok:
            print("nav error:", nav.error.message if nav.error else nav)
            # Try home as fallback to at least see a post in the timeline.
            print("falling back to home timeline...")
            await broker.navigate("https://x.com/home")

        await asyncio.sleep(5)  # hydrate

        print("\n=== observe ===")
        obs = await broker.observe()
        if not obs.ok:
            print("observe failed:", obs.error.message if obs.error else obs)
            return 1
        data = obs.data or {}
        print(f"url={data.get('url')}")
        print(f"title={data.get('title')!r}")
        print(f"interactive={data.get('interactive_elements')} total={data.get('total_elements')}")

        targets = data.get("targets") or []
        print(f"\ntargets ({len(targets)}):")
        for i, t in enumerate(targets[:50]):
            name = (t.get("name") or "")[:70]
            print(f"  [{i:2}] role={t.get('role'):10} name={name!r}")

        # Probe key X post data-testids via query_attr.
        print("\n=== query_attr probes (X post data-testids) ===")
        probes = [
            ("[data-testid='tweetText']", "textContent"),
            ("[data-testid='User-Name']", "textContent"),
            ("[data-testid='tweetText']", "data-testid"),
            ("article", "role"),
            ("time", "datetime"),
            ("a[href*='/status/']", "href"),
            ("[data-testid='like']", "aria-label"),
            ("[data-testid='reply']", "aria-label"),
            ("[data-testid='retweet']", "aria-label"),
            ("[data-testid='bookmark']", "aria-label"),
        ]
        for sel, attr in probes:
            r = await broker.query_attr(sel, attr)
            val = r.data.get("value") if (r.ok and r.data) else None
            val_s = (str(val)[:90] if val else repr(val))
            print(f"  {sel:42}[{attr:12}] = {val_s}")

    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
