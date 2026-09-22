"""M4b live diagnostic: which article carries the tweetPhotos on the reply
permalink? Distinguishes 'images dropped at submit' from 'verifier counted
the parent article'. Read-only. Output to stdout via file to dodge transport
shutdown noise."""

from __future__ import annotations

import asyncio
import json

from webwire.dispatcher import Dispatcher

REPLY = "https://x.com/infaag/status/2102493233989529629"


async def main() -> None:
    d = Dispatcher()
    started = await d.start()
    lines: list[str] = []
    try:
        if not started.ok:
            lines.append(f"start failed: {started.error}")
        else:
            sb = d.session_manager.sb
        await sb.navigate(REPLY, wait_until="domcontentloaded")
        await asyncio.sleep(5)
        cdp = sb._controller._cdp
        expr = (
            "(function(){var arts=document.querySelectorAll('article');"
            "return Array.from(arts).map(function(a){"
            "var link=a.querySelector(\"a[href*='/status/']\");"
            "return {href: link?link.getAttribute('href'):null,"
            "photos: a.querySelectorAll(\"[data-testid='tweetPhoto']\").length};});})()"
        )
        r = await cdp.evaluate(expr)
        arts = r.data.get("result", {}).get("value") or []
        for a in arts[:5]:
            lines.append(f"article: {a.get('href')} | tweetPhotos: {a.get('photos')}")
        lines.append(f"total tweetPhotos on page: {sum(a.get('photos', 0) for a in arts)}")
    finally:
        await d.stop()
    with open(".webwire/m4b_diag.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
