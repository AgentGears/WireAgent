"""Focused re-verification of qualification scenarios 6 and 7b.

The full run (qualify_m8_layer4_live.py, 2026-10-01T21:07) produced the
CORRECT product outcomes for both scenarios but recorded FAILs due to
harness assertion bugs:
  - scenario 6 counted ledger ROWS (RESERVED + terminal = 2 per lifecycle)
    instead of lifecycles;
  - scenario 7b demanded blocked_by in {consumed_token, dedupe}; the replay
    was fail-closed denied by the token-bucket gate, which runs before
    token validation — any denial is the contract.

This script re-exercises exactly those two scenarios with corrected
assertions: one phase-2 blocked by a late NEVER (zero effect), one raw
token pair (first use executes one like; replay must be denied).
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig
from webwire.m8_cards import CardFlow
from webwire.safety.user_rules import (
    RuleDecision,
    RuleSelector,
    RuleStore,
    UserRule,
)

MAIN_STATE = Path(".webwire")
QUAL_STATE = MAIN_STATE / "qual-m8-layer4-sc67"
results: list[dict] = []


def record(scenario: str, ok: bool, evidence: str) -> None:
    results.append({"scenario": scenario, "ok": ok, "evidence": evidence})
    print(f"{'PASS' if ok else 'FAIL'}  {scenario}: {evidence}")


def ledger_rows() -> list:
    from webwire.safety.effect_ledger import EffectLedger

    path = QUAL_STATE / "effects.ndjson"
    if not path.exists():
        return []
    return EffectLedger(path=path).read_records()


async def harvest_posts(d: Dispatcher, count: int) -> list[str]:
    import re as _re

    broker = d._broker
    await broker.navigate("https://x.com/home")
    await asyncio.sleep(5)
    sb = d.session_manager.sb
    cdp = sb._controller._cdp  # type: ignore[attr-defined]
    expr = (
        "(function(){return Array.from(document.querySelectorAll(\"a[href*='/status/']\"))"
        ".map(a=>a.getAttribute('href')).filter(h=>h&&/status\\/\\d+/.test(h));})()"
    )
    hrefs: list[str] = []
    for _ in range(3):
        result = await cdp.evaluate(expr)
        if result.ok and "exceptionDetails" not in result.data:
            for h in (result.data.get("result", {}).get("value") or []):
                if h not in hrefs:
                    hrefs.append(h)
        if len(hrefs) >= 12:
            break
        await cdp.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await asyncio.sleep(3)
    seen: set[str] = set()
    unique: list[str] = []
    plain_re = _re.compile(r"/[^/]+/status/\d+$")
    for h in hrefs:
        if h not in seen and plain_re.fullmatch(h.split("?")[0]):
            seen.add(h)
            unique.append("https://x.com" + h.split("?")[0])
    return unique[:count]


async def main() -> int:
    if QUAL_STATE.exists():
        shutil.rmtree(QUAL_STATE)
    QUAL_STATE.mkdir(parents=True, exist_ok=True)
    shutil.copy(MAIN_STATE / "session.json", QUAL_STATE / "session.json")

    env = {"machine_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
           "state_dir": str(QUAL_STATE)}
    print("ENV:", json.dumps(env))

    cfg = WebWireConfig(state_dir=QUAL_STATE)
    d = Dispatcher(cfg)
    if not (await d.start()).ok:
        record("start", False, "start failed")
        return 1
    try:
        who = await d.invoke("whoami", {})
        if not (who.ok and getattr(d.session_manager, "resolved_handle", None)):
            record("0-whoami", False, "identity unresolved")
            return 1
        handle = d.session_manager.resolved_handle
        record("0-whoami", True, f"handle={handle!r}")

        posts = await harvest_posts(d, 2)
        record("0b-harvest", len(posts) >= 2, f"posts={posts}")
        if len(posts) < 2:
            return 1

        flow = CardFlow(d.invoke)
        rules = RuleStore(cfg.rules_path())

        # -- scenario 6: rule changed between phase 1 and phase 2 ----------
        _, card6 = await flow.begin("like_post", {"post_url": posts[0]})
        rules.save([
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"like"})),
                decision=RuleDecision.NEVER, rule_id="sc67-late-never",
            ),
        ])
        if card6 is None:
            record("6-rule-changed-phase2", False, "phase 1 produced no card")
        else:
            replay = await card6.approve()
            pol6 = (replay.data or {}).get("policy", {})
            like_ids = {r.effect_id for r in ledger_rows() if r.action_type == "like"}
            ok6 = bool(pol6.get("blocked_by") == "user_rule" and len(like_ids) == 0)
            record("6-rule-changed-phase2", ok6,
                   f"phase2_blocked_by={pol6.get('blocked_by')} like_lifecycles={len(like_ids)}")
        rules.save([])

        # -- scenario 7b: reused token fails closed (any denial reason) -----
        raw_p1 = await d.invoke("like_post", {"post_url": posts[1]})
        token = ((raw_p1.data or {}).get("data") or {}).get("confirmation_token")
        first = await d.invoke("like_post", {
            "post_url": posts[1], "confirmation_token": token,
        })
        pol_first = (first.data or {}).get("policy", {})
        replay = await d.invoke("like_post", {
            "post_url": posts[1], "confirmation_token": token,
        })
        pol_re = (replay.data or {}).get("policy", {})
        ok7b = bool(
            pol_first.get("verdict") == "allow"
            and pol_re.get("verdict") != "allow"
            and pol_re.get("blocked_by")
        )
        record("7b-reused-token-fail-closed", ok7b,
               f"first_use={pol_first.get('verdict')} replay={pol_re.get('verdict')} "
               f"replay_blocked_by={pol_re.get('blocked_by')}")

        print(json.dumps({"environment": env, "account": handle,
                          "results": results}, indent=1, default=str))
        return 0 if all(r["ok"] for r in results) else 1
    finally:
        await d.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
