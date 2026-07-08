"""Diagnostic: dump what observe() sees on live X, to debug whoami.

Runs attach -> navigate x.com/home -> observe -> print the raw target list
(names + roles) so we can see what X's real DOM exposes for account identity.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)

    print("=== starting (attach) ===")
    r = await d.start()
    if not r.ok:
        print("start failed:", r.error.message if r.error else r)
        return 1

    try:
        # Direct broker access for diagnosis.
        broker = d._broker
        assert broker is not None

        print("\n=== navigate x.com/home ===")
        nav = await broker.navigate("https://x.com/home")
        print(f"navigate ok={nav.ok}")

        # X is a React SPA — content isn't ready at domcontentloaded. Wait for
        # networkidle and a short settle, then re-observe. Also probe the DOM
        # directly to see if the page actually rendered content.
        print("\n=== waiting for SPA hydration (5s) ===")
        await asyncio.sleep(5)

        # Direct DOM probe via the underlying page (diagnostic only —
        # capabilities would never do this; they use the broker).
        sb = d.session_manager.sb
        if sb is not None and sb._page is not None:
            page = sb._page
            try:
                # Use the engine page's eval if available.
                ep = getattr(page, "engine_page", page)
                body_len = await ep.evaluate("document.body ? document.body.innerHTML.length : 0")
                h1 = await ep.evaluate("document.title")
                has_login = await ep.evaluate(
                    "Array.from(document.querySelectorAll('input')).some(i => i.type === 'password')"
                )
                print(f"DOM probe: document.title={h1!r} body_innerHTML_len={body_len} has_password_input={has_login}")
            except Exception as exc:
                print(f"DOM probe failed: {exc!r}")

        print("\n=== observe (after wait) ===")
        obs = await broker.observe()
        print(f"observe ok={obs.ok}")
        if not obs.ok:
            print("observe error:", obs.error.message if obs.error else obs)
            return 1

        data = obs.data or {}
        print(f"\nurl: {data.get('url')}")
        print(f"title: {data.get('title')}")
        print(f"interactive_elements: {data.get('interactive_elements')}")
        print(f"total_elements: {data.get('total_elements')}")

        targets = data.get("targets", []) or []
        print(f"\ntargets ({len(targets)}):")
        for i, t in enumerate(targets):
            name = (t.get("name") or "")[:60]
            print(f"  [{i:2}] role={t.get('role'):10} action_hint={t.get('action_hint'):14} name={name!r}")

        # Specifically look for anything handle-like.
        print("\n=== handle-like candidates ===")
        for t in targets:
            name = (t.get("name") or "").strip()
            if name.startswith("@"):
                print(f"  @-handle: {name!r} (role={t.get('role')})")
            elif name and t.get("role") == "link":
                tentative = name.lstrip("@")
                if tentative and all(c.isalnum() or c == "_" for c in tentative) and 1 <= len(tentative) <= 15:
                    print(f"  handle-like link: {name!r} (role={t.get('role')})")

        # Now run the actual whoami capability.
        print("\n=== whoami capability ===")
        who = await d.invoke("whoami")
        print(f"whoami ok={who.ok}")
        if who.ok:
            print(f"identity: {json.dumps(who.data, indent=2)}")
        else:
            print(f"failure_category: {who.failure_category.value if who.failure_category else None}")
            print(f"error: {who.error.message if who.error else None}")

    finally:
        print("\n=== stopping ===")
        await d.stop()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
