"""Phase 2 probe: examine a profile timeline's DOM to ground the fan-out design.

Key questions:
1. Are individual posts each their own <article>? (Determines enumeration strategy)
2. How many posts are visible on initial load? (Before scroll)
3. What's the post-href pattern on a profile? (/<handle>/status/<id>)
4. Does scrolling load more? (infinite scroll / pagination)
5. Are there tabs (Posts / Replies / Media / Likes)? (scope selection)

Uses CDP evaluate directly (via the broker's controller) to enumerate all
articles + their status hrefs, since query_attr only returns the first match.
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
        print("start failed:", r.error.message if r.error else r)
        return 1
    try:
        broker = d._broker
        sb = d.session_manager.sb
        cdp = sb._controller._cdp  # type: ignore[attr-defined]

        # Use jack's profile — public, high-volume, stable.
        profile_url = "https://x.com/jack"
        print(f"=== navigate {profile_url} ===")
        await broker.navigate(profile_url)
        await asyncio.sleep(5)  # hydrate

        # 1. Tab presence — Posts / Replies / Media / Likes
        print("\n=== profile tabs ===")
        tab_expr = (
            "(function(){return Array.from(document.querySelectorAll('a[role=tab]'))"
            ".map(a=>({text:a.innerText, href:a.getAttribute('href'), selected:a.getAttribute('aria-selected')}));})()"
        )
        tr = await cdp.evaluate(tab_expr)
        tabs = tr.data.get("result", {}).get("value") if (tr.ok and tr.data) else []
        for t in (tabs or []):
            print(f"  tab: {t}")

        # 2. Enumerate articles + their status hrefs
        print("\n=== articles on initial load ===")
        enum_expr = (
            "(function(){"
            "var arts=document.querySelectorAll('article');"
            "return Array.from(arts).map(function(a){"
            "  var link=a.querySelector(\"a[href*='/status/']\");"
            "  var time=a.querySelector('time');"
            "  var text=a.querySelector(\"[data-testid='tweetText']\");"
            "  return {"
            "    href: link?link.getAttribute('href'):null,"
            "    time: time?time.getAttribute('datetime'):null,"
            "    text: text?text.innerText.slice(0,60):null"
            "  };"
            "});"
            "})()"
        )
        er = await cdp.evaluate(enum_expr)
        posts = er.data.get("result", {}).get("value") if (er.ok and er.data) else []
        print(f"articles found: {len(posts)}")
        for i, p in enumerate((posts or [])[:12]):
            print(f"  [{i}] href={p.get('href')} time={p.get('time')}")
            print(f"       text={p.get('text')!r}")

        # 3. Scroll to load more, then re-count
        print("\n=== after 3 scrolls ===")
        for _ in range(3):
            await cdp.evaluate("window.scrollBy(0, 3000)")
            await asyncio.sleep(2)
        er2 = await cdp.evaluate(enum_expr)
        posts2 = er2.data.get("result", {}).get("value") if (er2.ok and er2.data) else []
        print(f"articles after scroll: {len(posts2)}")

        # 4. Observe snapshot — does the AX tree show the posts too?
        print("\n=== observe() on profile ===")
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
