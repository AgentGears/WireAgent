"""M4b verifier debug: on the reply permalink, dump per-article hrefs, the
scoped selector's view, and what the new expressions actually resolve to."""

from __future__ import annotations

import asyncio

from webwire.dispatcher import Dispatcher

REPLY = "https://x.com/infaag/status/2102493233989529629"
REPLY_ID = "2102493233989529629"
TEXT = "Agent-WebWire M4b reply_multi_image test — two images."

DUMP = (
    "(function(){"
    "var arts=document.querySelectorAll('article');"
    "var out=[];"
    "for(var i=0;i<arts.length;i++){"
    "var links=arts[i].querySelectorAll(\"a[href*='/status/']\");"
    "var hrefs=[];for(var j=0;j<links.length;j++){hrefs.push(links[j].getAttribute('href'));}"
    "var scoped=arts[i].querySelector(\"a[href*='/status/" + REPLY_ID + "']\");"
    "var t=arts[i].querySelector(\"[data-testid='tweetText']\");"
    "out.push({idx:i,hrefs:hrefs.slice(0,4),scoped_match:!!scoped,"
    "photos:arts[i].querySelectorAll(\"[data-testid='tweetPhoto']\").length,"
    "text:t?t.innerText.slice(0,60):null});}"
    "return JSON.stringify(out);})()"
)


async def main() -> None:
    d = Dispatcher()
    started = await d.start()
    lines = []
    try:
        if not started.ok:
            lines.append("start failed")
        else:
            sb = d.session_manager.sb
            nav = await sb.navigate(REPLY, wait_until="domcontentloaded")
            lines.append(f"nav ok={nav.ok}")
            await asyncio.sleep(5)
            cdp = sb._controller._cdp
            r = await cdp.evaluate(DUMP)
            import json
            arts = json.loads(r.data.get("result", {}).get("value") or "[]")
            for a in arts:
                lines.append(
                    f"art[{a['idx']}] scoped={a['scoped_match']} photos={a['photos']} "
                    f"text={a['text']!r} hrefs={a['hrefs']}"
                )
    finally:
        await d.stop()
    with open(".webwire/m4b_debug.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
