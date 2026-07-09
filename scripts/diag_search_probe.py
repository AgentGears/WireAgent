"""Phase 2c probe: examine X search results DOM.

Key questions:
1. Are search results each their own <article>? (Same as profile/thread?)
2. What tabs exist? (Top/Latest/People/Media/etc.)
3. How does the search URL work? (q= param, f= filter)
4. Does enumerate_posts work on search results?
5. Coverage: how many results visible, scroll behavior?
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
        sb = d.session_manager.sb
        cdp = sb._controller._cdp  # type: ignore[attr-defined]

        # Search for a stable term.
        search_url = "https://x.com/search?q=hello%20world&src=typed_query&f=top"
        print(f"=== navigate {search_url} ===")
        await broker.navigate(search_url)
        await asyncio.sleep(5)

        # 1. Tabs
        print("\n=== search tabs ===")
        tab_expr = (
            "(function(){return Array.from(document.querySelectorAll('a[role=tab]'))"
            ".map(a=>({text:a.innerText,href:a.getAttribute('href'),selected:a.getAttribute('aria-selected')}));})()"
        )
        tr = await cdp.evaluate(tab_expr)
        tabs = tr.data.get("result", {}).get("value") if (tr.ok and tr.data) else []
        for t in (tabs or []):
            print(f"  tab: {t}")

        # 2. Articles
        print("\n=== articles on search page ===")
        enum_r = await broker.enumerate_posts()
        posts = (enum_r.data or {}).get("posts", []) if enum_r.ok else []
        print(f"enumerate_posts count: {len(posts)}")
        for i, p in enumerate((posts or [])[:8]):
            print(f"  [{i}] href={p.get('href')} text={repr((p.get('text') or '')[:50])}")

        # 3. Scroll test
        print("\n=== after 2 scrolls ===")
        for _ in range(2):
            await broker.scroll(3000)
            await asyncio.sleep(2)
        enum_r2 = await broker.enumerate_posts()
        posts2 = (enum_r2.data or {}).get("posts", []) if enum_r2.ok else []
        print(f"articles after scroll: {len(posts2)}")

        # 4. Observe
        print("\n=== observe() ===")
        obs = await broker.observe()
        if obs.ok:
            data = obs.data or {}
            print(f"url={data.get('url')} title={data.get('title')!r}")
            print(f"interactive={data.get('interactive_elements')} total={data.get('total_elements')}")

    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
