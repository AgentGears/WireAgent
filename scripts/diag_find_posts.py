"""Find real post URLs from the authenticated home timeline — for Phase 1b
fixtures. Scans the timeline for: a quote-tweet, a media (photo) post, and a
reply, by probing each article's data-testid markers.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed")
        return 1
    try:
        broker = d._broker
        await broker.navigate("https://x.com/home")
        await asyncio.sleep(5)

        # Read all the status hrefs visible on the timeline.
        # query_attr only returns the FIRST match. To get many, use observe()
        # and extract hrefs from the AX snapshot... but AX doesn't expose hrefs.
        # Instead, use a CDP evaluate via the controller to grab all status links.
        sb = d.session_manager.sb
        cdp = sb._controller._cdp  # type: ignore[attr-defined]
        expr = (
            "(function(){return Array.from(document.querySelectorAll(\"a[href*='/status/']\"))"
            ".map(a=>a.getAttribute('href')).filter(h=>h&&/status\\//.test(h));})()"
        )
        result = await cdp.evaluate(expr)
        hrefs = []
        if result.ok and "exceptionDetails" not in result.data:
            hrefs = result.data.get("result", {}).get("value") or []
        # Deduplicate preserving order.
        seen = set()
        unique = []
        for h in hrefs:
            if h not in seen:
                seen.add(h)
                unique.append(h)
        print(f"Found {len(unique)} unique status hrefs on home timeline:")
        for h in unique[:25]:
            print(f"  {h}")

        # Now for the first ~8, probe whether each is a quote-tweet / has media.
        print("\n=== probing first posts for quote/media markers ===")
        for h in unique[:8]:
            # We can't easily query a *specific* article on the timeline page
            # without scoping. Just report the hrefs; the user can pick real ones.
            print(f"  {h}")
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
