"""M4c live diagnostic: what does the QUOTE composer's DOM contain after one
image attach? Determines which imgs count_attachments sees (uploads = blob:
src; the quote card's avatar/preview = https src). Aborts without submit —
no public side effect."""

from __future__ import annotations

import asyncio
import json

from webwire.dispatcher import Dispatcher

TARGET = "https://x.com/infaag/status/2102451358305771541"
TARGET_ID = "2102451358305771541"

DUMP = (
    "(function(){"
    "var out={};"
    "var att=document.querySelector(\"[data-testid='attachments']\");"
    "out.attachments_container=!!att;"
    "if(att){out.att_imgs=att.querySelectorAll('img').length;}"
    "var ta=document.querySelector(\"[data-testid='tweetTextarea_0']\");"
    "out.textarea0=!!ta;"
    "var form=document.querySelector('form');"
    "out.form=!!form;"
    "if(form){"
    "var imgs=form.querySelectorAll('img');"
    "out.form_img_count=imgs.length;"
    "out.form_img_srcs=Array.from(imgs).map(function(i){"
    "return (i.getAttribute('src')||'').slice(0,40);}).slice(0,6);"
    "out.form_blob_count=form.querySelectorAll(\"img[src*='blob:']\").length;}"
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
            await sb.navigate(TARGET, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            wb = d._write_kernel._write_broker_factory()
            open_r = await wb.open_quote_on_target(TARGET, TARGET_ID)
            lines.append(f"open_quote ok={open_r.ok}")
            fill_r = await wb.fill_quote_composer("diag — will abort, no submit")
            lines.append(f"fill ok={fill_r.ok}")
            attach_r = await wb.attach_media(".webwire/test-media/test_red.png")
            lines.append(f"attach ok={attach_r.ok}")
            await asyncio.sleep(2)
            cdp = sb._controller._cdp
            r = await cdp.evaluate(DUMP)
            data = json.loads(r.data.get("result", {}).get("value") or "{}")
            lines.append(json.dumps(data, indent=1))
            cnt = await wb.count_attachments()
            lines.append(f"count_attachments says: {(cnt.data or {}).get('count')}")
            await wb.close_composer()
            lines.append("composer closed — no submit")
    finally:
        await d.stop()
    with open(".webwire/m4c_diag.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
