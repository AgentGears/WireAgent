"""Classify real timeline posts: quote-tweet, media-only, reply, or plain text.
Uses the real hrefs found on the authenticated home timeline.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig

REAL_HREFS = [
    "https://x.com/rosemag_3/status/2074579191438197113",
    "https://x.com/dmwo_/status/2074569515678011871",
    "https://x.com/gorgeous4ew/status/2074848054104990038",
]


async def classify(broker, url: str) -> None:
    print(f"\n=== {url} ===")
    await broker.navigate(url)
    await asyncio.sleep(4)
    art = await broker.query_attr("article", "role")
    art_ok = art.ok and art.data and art.data.get("value") == "article"
    print(f"  article: {art_ok}")

    tt = await broker.query_text("[data-testid='tweetText']")
    tt_v = (tt.data.get("value") if (tt.ok and tt.data) else None)
    print(f"  text: {repr(tt_v)[:80] if tt_v else tt_v!r}")

    qt = await broker.query_attr("[data-testid='quoteTweet']", "role")
    qt_v = qt.data.get("value") if (qt.ok and qt.data) else None
    print(f"  quoteTweet: {qt_v!r}")

    media = await broker.query_attr("[data-testid='tweetPhoto']", "role")
    media_v = media.data.get("value") if (media.ok and media.data) else None
    print(f"  tweetPhoto: {media_v is not None}")

    # reply indicator: "Replying to @x" text
    replying = await broker.query_text("#tweet-ancestor-root, [data-testid='conversation']")
    # fallback: check for the reply context via a broader selector
    sb_href = None
    href = await broker.query_attr("a[href*='/status/']", "href")
    if href.ok and href.data:
        sb_href = href.data.get("value")
    print(f"  href: {sb_href!r}")

    # classify
    tags = []
    if qt_v is not None:
        tags.append("QUOTE-TWEET")
    if media_v is not None:
        tags.append("MEDIA")
    if not tt_v and art_ok:
        tags.append("NO-TEXT")
    print(f"  CLASS: {', '.join(tags) if tags else 'plain text'}")


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed")
        return 1
    try:
        broker = d._broker
        for url in REAL_HREFS:
            try:
                await classify(broker, url)
            except Exception as exc:  # noqa: BLE001
                print(f"  error: {exc!r}")
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
