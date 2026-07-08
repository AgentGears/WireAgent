"""Phase 1b DOM probe — examine quote-tweet, media-only, reply, and
unavailable post shapes against real X, in one browser session.

For each post URL, dumps: url, title, article presence, tweetText innerText,
the quote-tweet container presence, and key data-testids. This grounds the
edge-case contracts before writing code.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


# Test fixtures (well-known stable posts):
#  - quote-tweet: pick one with a visible quoted card
#  - media-only: a post that is an image with no text (or minimal)
#  - reply: a post that is a reply (shows "Replying to @x" + parent context)
#  - unavailable: a nonexistent/deleted status id
POSTS = {
    "simple": "https://x.com/X/status/20",            # jack's first (baseline)
    "quote_tweet": "https://x.com/X/status/1889625598977519704",  # X官方 quote-ish; adjust if needed
    "media_only": "https://x.com/NASA/status/1889630881993642400",  # NASA image post; adjust
    "reply": "https://x.com/X/status/1889625598977519704",  # placeholder; will adjust
    "unavailable": "https://x.com/X/status/9999999999999999999",  # almost certainly nonexistent
}


async def probe_one(broker, label: str, url: str) -> None:
    print(f"\n{'='*60}\n{label}: {url}\n{'='*60}")
    nav = await broker.navigate(url)
    if not nav.ok:
        print(f"  navigate FAILED: {nav.error.message if nav.error else nav}")
        return
    await asyncio.sleep(4)  # hydrate

    obs = await broker.observe()
    data = obs.data or {} if obs.ok else {}
    print(f"  url: {data.get('url')}")
    print(f"  title: {data.get('title')!r}")
    print(f"  interactive: {data.get('interactive_elements')}")

    # article container
    art = await broker.query_attr("article", "role")
    art_v = art.data.get("value") if (art.ok and art.data) else None
    print(f"  article[role]: {art_v!r}")

    # tweetText
    tt = await broker.query_text("[data-testid='tweetText']")
    tt_v = (tt.data.get("value") if (tt.ok and tt.data) else None)
    print(f"  tweetText innerText: {repr(tt_v)[:100] if tt_v else tt_v!r}")

    # quote-tweet container — X uses data-testid='quoteTweet'
    qt = await broker.query_attr("[data-testid='quoteTweet']", "role")
    qt_v = qt.data.get("value") if (qt.ok and qt.data) else None
    print(f"  quoteTweet[role]: {qt_v!r}")
    # Also check the quote-tweet's own tweetText
    qtt = await broker.query_text("[data-testid='quoteTweet'] [data-testid='tweetText']")
    qtt_v = (qtt.data.get("value") if (qtt.ok and qtt.data) else None)
    print(f"  quoteTweet.tweetText: {repr(qtt_v)[:100] if qtt_v else qtt_v!r}")

    # status href (author + post_id)
    href = await broker.query_attr("a[href*='/status/']", "href")
    href_v = href.data.get("value") if (href.ok and href.data) else None
    print(f"  status href: {href_v!r}")

    # time
    t = await broker.query_attr("article time", "datetime")
    t_v = t.data.get("value") if (t.ok and t.data) else None
    print(f"  time[datetime]: {t_v!r}")

    # media presence — X uses data-testid='tweetPhoto' or video components
    media = await broker.query_attr("[data-testid='tweetPhoto']", "role")
    media_v = media.data.get("value") if (media.ok and media.data) else None
    print(f"  tweetPhoto present: {media_v is not None}")

    # a few engagement buttons
    for sel, name in (
        ("[data-testid='reply']", "reply"),
        ("[data-testid='like']", "like"),
    ):
        r = await broker.query_attr(sel, "aria-label")
        v = r.data.get("value") if (r.ok and r.data) else None
        print(f"  {name} aria-label: {v!r}")


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1
    try:
        broker = d._broker
        for label, url in POSTS.items():
            try:
                await probe_one(broker, label, url)
            except Exception as exc:  # noqa: BLE001
                print(f"  probe error: {exc!r}")
    finally:
        await d.stop()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
