"""Login helper (manual signal) — launches Super-Browser Chromium, navigates to
X, and HOLDS the browser open until you create a signal file.

This avoids timeout/polling friction. You log in at your own pace, then signal.

Usage:
  1. python scripts/login_manual.py
  2. Log into X in the window that opens.
  3. In another terminal: touch .webwire/login_done   (or create that file)
  4. This script detects the file, runs whoami (checkpoint), saves session, exits.

If whoami fails after you logged in, the cookies are still saved on stop if the
session looks authenticated; re-run the smoke to verify.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    signal = cfg.state_dir / "login_done"
    # Clear any stale signal.
    signal.unlink(missing_ok=True)

    d = Dispatcher(cfg)
    print("=== launching Super-Browser Chromium ===")
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1
    print(f"started: ownership={r.data.get('ownership') if r.data else '?'}, "
          f"session={r.data.get('session') if r.data else '?'}")

    broker = d._broker
    assert broker is not None
    await broker.navigate("https://x.com/home")

    print("\n>>> Browser is open on X. Log in if prompted.")
    print(f">>> When logged in, create this file: {signal}")
    print(">>> (e.g. run in another terminal: touch .webwire/login_done)")
    print(">>> This script will then checkpoint the session and exit.\n")

    # Wait for the signal file (poll every 2s, up to 30 min).
    for i in range(900):
        await asyncio.sleep(2)
        if signal.exists():
            print(f"\nSignal received after ~{i*2}s. Running whoami to checkpoint...")
            who = await d.invoke("whoami")
            if who.ok:
                print(f"whoami OK: {who.data}")
            else:
                fc = who.failure_category.value if who.failure_category else None
                print(f"whoami failed (fc={fc}): {who.error.message if who.error else who}")
                print("Cookies will still be saved on stop if the page is authenticated.")
            await d.stop()
            signal.unlink(missing_ok=True)
            sp = cfg.session_path()
            print(f"stop() complete. session.json exists: {sp.exists()}")
            if sp.exists():
                print(f"Session checkpointed to {sp}")
            return 0

    print("\n30-minute wait elapsed with no signal. Exiting; no checkpoint.")
    await d.stop()
    return 2


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
