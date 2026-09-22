"""Live selector battery (P1, 2026-09-22): which candidate selectors exist on
TODAY'S x.com/home DOM? Ground truth for the selector_readiness probe refresh.
Diagnostic script — uses raw CDP like the other diag_* scripts."""

from __future__ import annotations

import asyncio
import json
import sys

from webwire.dispatcher import Dispatcher

CANDIDATES = {
    "main_content": [
        "[data-testid='primaryColumn']",
        "main[role='main']",
        "#react-root",
        "article",
        "[data-testid='tweetText']",
        "header[role='banner']",
    ],
    "navigation": [
        "nav[role='navigation']",
        "[data-testid='AppTabBar_Explore_Link']",
        "[data-testid='AppTabBar_Home_Link']",
        "a[aria-label='Home']",
        "a[aria-label][role='link']",
    ],
    "account": [
        "[data-testid='SideNav_AccountSwitcher_Button']",
        "a[data-testid='AppTabBar_Profile_Link']",
        "[data-testid='TabsLink']",
    ],
    "composer_logged_in_signal": [
        "[data-testid='tweetButtonInline']",
        "[data-testid='tweetButton']",
        "a[data-testid='SideNav_NewTweet_Button']",
        "[data-testid='toolBar']",
    ],
}


async def main() -> int:
    d = Dispatcher()
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error)
        return 1
    try:
        sb = d.session_manager.sb
        await sb.navigate("https://x.com/home", wait_until="domcontentloaded")
        await asyncio.sleep(5)  # hydrate (codebase convention)

        results: dict[str, dict[str, bool]] = {}
        for group, selectors in CANDIDATES.items():
            results[group] = {}
            for sel in selectors:
                safe = sel.replace("\\", "\\\\").replace("'", "\\'")
                expr = (
                    f"(function(){{return document.querySelector('{safe}')!==null;}})()"
                )
                try:
                    res = await sb._controller._cdp.evaluate(expr)  # type: ignore[attr-defined]
                    ok = res.ok and "exceptionDetails" not in res.data
                    val = res.data.get("result", {}).get("value") if ok else None
                    results[group][sel] = bool(val)
                except Exception as exc:  # noqa: BLE001
                    results[group][sel] = f"ERROR: {exc!r}"
        print(json.dumps(results, indent=1))
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
