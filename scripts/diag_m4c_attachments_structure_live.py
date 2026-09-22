"""M4c diagnostic 2: structure of the attachments container after ONE upload
in the QUOTE composer vs the REPLY composer. Goal: find the per-item element
that equals the upload count in both (imgs-per-item may differ). Aborts
without submit."""

from __future__ import annotations

import asyncio
import json

from webwire.dispatcher import Dispatcher

TARGET = "https://x.com/infaag/status/2102451358305771541"
TARGET_ID = "2102451358305771541"

DUMP = (
    "(function(){"
    "var att=document.querySelector(\"[data-testid='attachments']\");"
    "if(!att){return JSON.stringify({container:false});}"
    "var kids=att.children;"
    "var items=[];"
    "for(var i=0;i<kids.length;i++){"
    "items.push({tag:kids[i].tagName,testid:kids[i].getAttribute('data-testid'),"
    "cls:(kids[i].getAttribute('class')||'').slice(0,40),"
    "imgs:kids[i].querySelectorAll('img').length});}"
    "return JSON.stringify({container:true,children:items,"
    "total_imgs:att.querySelectorAll('img').length});})()"
)


async def probe(d: "Dispatcher", mode: str, lines: list[str]) -> None:
    sb = d.session_manager.sb
    await sb.navigate(TARGET, wait_until="domcontentloaded")
    await asyncio.sleep(5)
    wb = d._write_kernel._write_broker_factory()
    if mode == "quote":
        open_r = await wb.open_quote_on_target(TARGET, TARGET_ID)
    else:
        open_r = await wb.open_reply_on_target(TARGET, TARGET_ID)
    lines.append(f"[{mode}] open ok={open_r.ok}")
    if mode == "quote":
        await wb.fill_quote_composer("diag — will abort, no submit")
    else:
        await wb.fill_reply_composer("diag — will abort, no submit")
    attach_r = await wb.attach_media(".webwire/test-media/test_red.png")
    lines.append(f"[{mode}] attach ok={attach_r.ok}")
    await asyncio.sleep(2)
    cdp = sb._controller._cdp
    r = await cdp.evaluate(DUMP)
    lines.append(f"[{mode}] " + json.dumps(
        json.loads(r.data.get("result", {}).get("value") or "{}"), indent=1))
    cnt = await wb.count_attachments()
    lines.append(f"[{mode}] count_attachments: {(cnt.data or {}).get('count')}")
    await wb.close_composer()
    await asyncio.sleep(2)


async def main() -> None:
    d = Dispatcher()
    started = await d.start()
    lines = []
    try:
        if not started.ok:
            lines.append("start failed")
        else:
            await probe(d, "quote", lines)
            await probe(d, "reply", lines)
    finally:
        await d.stop()
    with open(".webwire/m4c_diag2.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
