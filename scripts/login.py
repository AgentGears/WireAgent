"""Login helper — launches Super-Browser's Chromium and holds it open until
you've logged into X, so the session cookie persists in the dedicated profile.

Run: python scripts/login.py
Then log into X in the window that opens. The script polls for the login wall
disappearing and exits once authenticated (or on Ctrl+C / timeout).
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

    print("=== launching Super-Browser Chromium (dedicated profile) ===")
    print(f"profile dir: {cfg.chrome_profile_path()}")
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1
    print("browser launched. Navigate to X and log in.")

    sb = d.session_manager.sb
    if sb is None or sb._page is None:
        print("no live page — cannot poll. Exiting.")
        await d.stop()
        return 1

    broker = d._broker
    assert broker is not None

    # Navigate to X home; it'll show login if not yet authenticated.
    await broker.navigate("https://x.com/home")
    print("\nA browser window is open on X. Log in if prompted.")
    print("Polling for authenticated state (checks every 5s, up to 5 min)...\n")

    last_url = ""
    for i in range(120):  # 10 min
        await asyncio.sleep(5)
        try:
            obs = await broker.observe()
            data = obs.data or {} if obs.ok else {}
            url = (data.get("url") or "").lower()
            title = (data.get("title") or "")
            interactive = data.get("interactive_elements", 0)
            # Authenticated heuristics (any one):
            #  (a) on /home with rich content and no "log in" title, OR
            #  (b) on /home with very high interactive count (timeline loaded),
            #      regardless of title (X sometimes titles blank), OR
            #  (c) url moved to a /home or /<handle> surface with >40 elements.
            title_low = title.lower()
            authenticated = (
                ("/home" in url and interactive > 30 and "log in" not in title_low)
                or ("/home" in url and interactive > 50)
                or (interactive > 40 and "x.com" in url and "log in" not in title_low
                    and "it's what's happening" not in title_low)
            )
            if url != last_url or i % 6 == 0:  # log url changes + every 30s
                print(f"[{i*5:4d}s] url={url} title={title!r} interactive={interactive} auth={authenticated}")
                last_url = url
            if authenticated:
                print(f"\nAUTHENTICATED at {i*5}s (login detector). Checkpointing session...")
                # The login detector confirmed an authenticated timeline; mark the
                # session manager authenticated so checkpoint_session() will save.
                # (Normal path: whoami success marks this. Bootstrap path: trust
                #  the detector since whoami may not yet parse live X DOM.)
                d.session_manager.mark_authenticated()
                cp = await d.session_manager.checkpoint_session()
                if cp.ok and cp.data and cp.data.get("checkpointed"):
                    print(f"Session checkpointed to {cfg.session_path()}")
                else:
                    print(f"Checkpoint skipped/failed: {cp.data if cp.data else cp.error}")
                # Also run whoami for diagnostic insight (may fail — that's a
                # parser issue, not a session issue; cookies are already saved).
                # Dump observe targets so we can see what X's authenticated DOM
                # actually exposes for identity parsing.
                print("\n=== observe targets on authenticated timeline (for parser diagnosis) ===")
                obs = await broker.observe()
                if obs.ok:
                    data = obs.data or {}
                    print(f"url={data.get('url')} title={data.get('title')!r} "
                          f"interactive={data.get('interactive_elements')}")
                    for j, t in enumerate((data.get("targets") or [])[:40]):
                        name = (t.get("name") or "")[:50]
                        print(f"  [{j:2}] role={t.get('role'):10} name={name!r}")
                who = await d.invoke("whoami")
                if who.ok:
                    print(f"whoami resolved: {who.data}")
                else:
                    print(f"whoami note (parser issue, not session): "
                          f"{who.error.message if who.error else who}")
                await d.stop()
                return 0
        except Exception as exc:  # noqa: BLE001
            print(f"[{i*5:4d}s] poll error: {exc!r}")

    print("\nTimeout (10 min) reached without detecting authentication.")
    print("If you logged in but this didn't detect it, just close the browser —")
    print("then re-run the smoke; the cookie may still be in this session.")
    await d.stop()
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
