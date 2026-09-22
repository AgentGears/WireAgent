"""delete_post stage diagnostic (read-mostly): navigate to the throwaway,
find the id-scoped article, click its caret, DUMP every menu item text
(WITHOUT clicking Delete), then navigate away. All JS built via json.dumps."""

from __future__ import annotations

import asyncio
import json

from webwire.dispatcher import Dispatcher

TARGET = "https://x.com/infaag/status/2102523479664804123"
TARGET_ID = "2102523479664804123"


def _js_article_probe() -> str:
    sel_link = json.dumps(f"a[href*='/status/{TARGET_ID}']")
    sel_caret = json.dumps("[data-testid='caret']")
    return (
        "(function(){var arts=document.querySelectorAll('article');"
        f"for(var i=0;i<arts.length;i++){{"
        f"if(!arts[i].querySelector({sel_link}))continue;"
        f"var c=arts[i].querySelector({sel_caret});"
        'return JSON.stringify({article:true,caret:!!c});}'
        'return JSON.stringify({article:false});})()'
    )


def _js_caret_click() -> str:
    sel_link = json.dumps(f"a[href*='/status/{TARGET_ID}']")
    sel_caret = json.dumps("[data-testid='caret']")
    return (
        "(function(){var arts=document.querySelectorAll('article');"
        f"for(var i=0;i<arts.length;i++){{"
        f"if(!arts[i].querySelector({sel_link}))continue;"
        f"var c=arts[i].querySelector({sel_caret});"
        "if(!c)return 'no_caret';c.click();return 'caret_clicked';}"
        "return 'no_article';})()"
    )


def _js_menu_dump() -> str:
    return (
        '(function(){'
        'var items=document.querySelectorAll("[role=\'menuitem\']");'
        'var texts=[];for(var i=0;i<items.length;i++){'
        'var t=(items[i].innerText||"").trim();if(t)texts.push(t);}'
        'var menus=document.querySelectorAll("[data-testid=\'Dropdown\']").length;'
        'return JSON.stringify({menuitem_count:items.length,'
        'dropdown_testids:menus,item_texts:texts});})()'
    )


async def main() -> None:
    d = Dispatcher()
    s = await d.start()
    lines = []
    try:
        if not s.ok:
            lines.append("start failed")
        else:
            sb = d.session_manager.sb
            wb = d._write_kernel._write_broker_factory()
            nav = await sb.navigate(TARGET, wait_until="domcontentloaded")
            lines.append(f"nav ok={nav.ok}")
            for attempt in range(5):
                r = await wb._delete_eval(_js_article_probe())
                lines.append(f"article probe[{attempt}]: {r.data if r.ok else r.error}")
                if r.ok and r.data and '"article":true' in str(r.data):
                    break
                await asyncio.sleep(1.5)
            r2 = await wb._delete_eval(_js_caret_click())
            lines.append(f"caret click: {r2.data if r2.ok else r2.error}")
            await asyncio.sleep(1.5)
            r3 = await wb._delete_eval(_js_menu_dump())
            lines.append(f"menu dump: {r3.data if r3.ok else r3.error}")
            await sb.navigate("https://x.com/home", wait_until="domcontentloaded")
            lines.append("dismissed (navigated home)")
    finally:
        await d.stop()
    with open(".webwire/delete_diag.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(str(x) for x in lines) + "\n")


if __name__ == "__main__":
    asyncio.run(main())
