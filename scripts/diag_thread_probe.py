"""Phase 2b probe: examine a reply thread's DOM structure.

Navigates to a post that has replies, dumps:
1. How many articles are on the page (target + ancestors + replies)
2. Which article is the "focused" target post (aria-label, data-testid)
3. How ancestors vs the target vs replies are distinguished in the DOM
4. The "Replying to @x" context indicator
5. Coverage: how many replies are visible, does scroll load more

Uses jack/status/20 — a high-engagement post (17K+ replies) guaranteed to have
a rich thread page.
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

        # jack/status/20 has 17K+ replies — rich thread structure guaranteed.
        thread_url = "https://x.com/jack/status/20"
        print(f"=== navigate {thread_url} ===")
        await broker.navigate(thread_url)
        await asyncio.sleep(5)

        # 1. Count articles + their status hrefs + positions
        print("\n=== articles on thread page ===")
        enum_expr = (
            "(function(){"
            "var arts=document.querySelectorAll('article');"
            "return Array.from(arts).slice(0,15).map(function(a,i){"
            "  var link=a.querySelector(\"a[href*='/status/']\");"
            "  var time=a.querySelector('time');"
            "  var text=a.querySelector(\"[data-testid='tweetText']\");"
            "  return {"
            "    index:i,"
            "    href: link?link.getAttribute('href'):null,"
            "    time: time?time.getAttribute('datetime'):null,"
            "    text: text?text.innerText.slice(0,50):null"
            "  };"
            "});"
            "})()"
        )
        er = await cdp.evaluate(enum_expr)
        posts = er.data.get("result", {}).get("value") if (er.ok and er.data) else []
        print(f"articles found (first 15): {len(posts)}")
        for p in (posts or []):
            print(f"  [{p.get('index')}] href={p.get('href')} time={p.get('time')}")
            print(f"       text={p.get('text')!r}")

        # 2. Look for thread-specific DOM markers
        print("\n=== thread structure markers ===")
        markers = [
            ("[data-testid='tweetAncestorAccessibility']", "ancestor accessibility"),
            ("[data-testid='tweet-Ancestor']", "tweet ancestor"),
            ("[data-testid='conversation']", "conversation"),
            ("[aria-label='See more replies']", "more replies"),
            ("[data-testid='tweetText']", "any tweetText"),
            ("[data-testid='primaryColumn'] article", "primary column article"),
        ]
        for sel, name in markers:
            count_expr = f"document.querySelectorAll(\"{sel}\").length"
            # Escape for evaluate
            safe = sel.replace("\\", "\\\\").replace("'", "\\'")
            count_expr = f"document.querySelectorAll('{safe}').length"
            cr = await cdp.evaluate(f"(function(){{return {count_expr};}})()")
            cnt = cr.data.get("result", {}).get("value") if (cr.ok and cr.data) else "?"
            print(f"  {name:30} ({sel:45}): {cnt}")

        # 3. Check for the focused/main tweet — X marks it differently
        print("\n=== focused post detection ===")
        focus_expr = (
            "(function(){"
            "  var main = document.querySelector('[data-testid=\"primaryColumn\"]');"
            "  if(!main) return 'no primary column';"
            "  var arts = main.querySelectorAll('article');"
            "  return 'primary column has ' + arts.length + ' articles';"
            "})()"
        )
        fr = await cdp.evaluate(focus_expr)
        focus = fr.data.get("result", {}).get("value") if (fr.ok and fr.data) else "?"
        print(f"  {focus}")

        # 4. Observe snapshot for the AX view
        print("\n=== observe() on thread page ===")
        obs = await broker.observe()
        if obs.ok:
            data = obs.data or {}
            print(f"url={data.get('url')} title={data.get('title')!r}")
            print(f"interactive={data.get('interactive_elements')} total={data.get('total_elements')}")
            # Look for "Replying to" or "Show more replies" in targets
            targets = data.get("targets", []) or []
            for t in targets:
                name = (t.get("name") or "")
                if "reply" in name.lower() or "replying" in name.lower() or "more" in name.lower():
                    print(f"  thread-related target: role={t.get('role')} name={name!r}")

    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
