"""M8 Layer 4 live-session qualification (frozen-scope exercise, not a test).

Runs the real Dispatcher with a REAL logged-in browser session through the
card surface. Each scenario that reaches phase 1+ uses a DISTINCT real post
harvested from the authenticated home timeline, because the kernel's dedupe
gate (correctly) denies same-target repeats within TTL before preview or
the rule gate ever run.

Scenarios:
  0. environment + whoami (verified actor identity)
  1. human ASK  -> render -> approve -> real effect (like; ledger human)
  2. human ASK  -> deny   -> zero effect (card on a different post)
  3. standing ALLOW (below ceiling) -> no card; rule attribution in the
     LEDGER via the fenced like action (bookmark is BEST_EFFORT and
     intentionally writes no ledger rows)
  4. NEVER -> denial BEFORE confirmation authority (fresh target)
  5. above-ceiling ALLOW (post) -> still asks; card denied, nothing posted
  6. rule changed between phase 1 and phase 2 -> reevaluation blocks
  7. malformed token / reused token / caller-supplied at the card boundary
  8. journal + ledger inspection: no raw confirmation authority persisted

Real effects (disclosed): THREE public likes on three ordinary timeline
posts (scenario 1's approved like; scenario 3's rule-auto like; scenario
7b's raw-route first use) and ONE private bookmark is NOT executed (the
standing-ALLOW scenario uses like for ledger attribution). Nothing is
posted; scenario 5's card is denied. Likes are not reversible through the
current capability surface.

State isolation: dedicated state dir with a COPY of the persisted session.
"""

from __future__ import annotations

import asyncio
import json
import platform
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
QUAL_STATE = MAIN_STATE / "qual-m8-layer4"

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
    """Real post URLs from the authenticated home timeline (the
    diag_find_posts idiom). Returns absolute URLs, own-posts excluded."""
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
    for h in hrefs:
        if h not in seen and not any(
            h.startswith(f"/{p}/") for p in ("home", "notifications", "messages", "explore", "i/")
        ):
            seen.add(h)
            unique.append("https://x.com" + h.split("?")[0])
    # keep only plain status permalinks (skip /analytics, /photos suffixes)
    plain = [u for u in unique if au_re.fullmatch(u)][:count]
    return plain


import re as _re

au_re = _re.compile(r"https://x\.com/[^/]+/status/\d+$")


async def main() -> int:
    if QUAL_STATE.exists():
        shutil.rmtree(QUAL_STATE)
    QUAL_STATE.mkdir(parents=True, exist_ok=True)
    src_session = MAIN_STATE / "session.json"
    if src_session.exists():
        shutil.copy(src_session, QUAL_STATE / "session.json")

    env = {
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "machine_clock": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "state_dir": str(QUAL_STATE),
        "session_source": str(src_session),
    }
    print("ENV:", json.dumps(env, indent=1))

    cfg = WebWireConfig(state_dir=QUAL_STATE)
    d = Dispatcher(cfg)
    print("=== dispatcher start (live browser) ===")
    started = await d.start()
    if not started.ok:
        record("start", False, f"start failed: {getattr(started.error, 'message', started)}")
        return 1
    try:
        # Empirical note: whoami identity resolution is INTERMITTENT across
        # page loads (hydration race; ~1-in-3 observed). Bounded retry in the
        # harness — recorded as a browser-behavior finding, not hidden.
        who = None
        for attempt in range(4):
            who = await d.invoke("whoami", {})
            actor = getattr(d.session_manager, "resolved_handle", None)
            if who.ok and actor:
                break
            await asyncio.sleep(8)
        handle = (who.data or {}).get("handle") if isinstance(who.data, dict) else None
        actor = getattr(d.session_manager, "resolved_handle", None)
        record("0-whoami", bool(who.ok and actor),
               f"whoami.ok={who.ok} handle={handle!r} resolved_handle={actor!r} attempts={attempt + 1}")
        if not (who.ok and actor):
            return 1

        posts = await harvest_posts(d, 4)
        record("0b-harvest", len(posts) >= 4, f"posts={[u.rsplit('/', 2)[-2] + '/' + u.rsplit('/', 1)[-1] for u in posts]}")
        if len(posts) < 4:
            return 1

        flow = CardFlow(d.invoke)
        rules = RuleStore(cfg.rules_path())

        # -- 1. human ASK -> render -> approve -> real effect --------------
        r1, card1 = await flow.begin("like_post", {"post_url": posts[0]})
        token_scrubbed = "confirmation_token" not in ((r1.data or {}).get("data") or {})
        ok1 = bool(card1 is not None and token_scrubbed)
        record("1a-ask-card", ok1,
               f"card={'yes' if card1 else 'NO'} summary={card1.summary if card1 else None!r} token_scrubbed={token_scrubbed}")
        if card1 is None:
            return 1
        print(card1.render_text())
        approved = await card1.approve()
        pol1 = (approved.data or {}).get("policy", {})
        led1 = [r for r in ledger_rows() if r.action_type == "like"]
        ok1b = bool(
            pol1.get("verdict") == "allow"
            and (approved.data or {}).get("trace", {}).get("execute_ok") is True
            and any(r.approver == "human" for r in led1)
        )
        record("1b-approve-effect", ok1b,
               f"verdict={pol1.get('verdict')} execute_ok={(approved.data or {}).get('trace', {}).get('execute_ok')} like_approvers={[r.approver for r in led1]}")

        # -- 2. human ASK -> deny -> zero effect (different post) ----------
        before2 = len(ledger_rows())
        r2, card2 = await flow.begin("like_post", {"post_url": posts[1]})
        if card2 is not None:
            print(card2.render_text())
            denied = card2.deny()
            after2 = len(ledger_rows())
            ok2 = bool(not denied.ok and after2 == before2)
            record("2-deny-zero-effect", ok2,
                   f"denied.ok={denied.ok} ledger_before={before2} after={after2}")
        else:
            record("2-deny-zero-effect", False,
                   f"no card; policy={(r2.data or {}).get('policy', {}).get('verdict')}/{(r2.data or {}).get('policy', {}).get('blocked_by')}")

        # -- 3. standing ALLOW (like, below ceiling) -> no card + ledger ----
        rules.save([
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"like"})),
                decision=RuleDecision.ALLOW, rule_id="qual-allow-like",
            ),
        ])
        r3, card3 = await flow.begin("like_post", {"post_url": posts[2]})
        pol3 = (r3.data or {}).get("policy", {})
        led3 = [r for r in ledger_rows() if r.action_type == "like"]
        ok3 = bool(
            card3 is None
            and pol3.get("verdict") == "allow"
            and any(r.approver == "rule:qual-allow-like" for r in led3)
        )
        record("3-standing-allow", ok3,
               f"card={'yes' if card3 else 'no'} verdict={pol3.get('verdict')} trace_approver={(r3.data or {}).get('trace', {}).get('approver')} like_approvers={[r.approver for r in led3]}")
        rules.save([])

        # -- 4. NEVER -> denial BEFORE confirmation authority ---------------
        rules.save([
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"like"})),
                decision=RuleDecision.NEVER, rule_id="qual-never-like",
            ),
        ])
        r4, card4 = await flow.begin("like_post", {"post_url": posts[3]})
        pol4 = (r4.data or {}).get("policy", {})
        data4 = (r4.data or {}).get("data", {})
        before4 = len(ledger_rows())
        ok4 = bool(
            card4 is None
            and pol4.get("blocked_by") == "user_rule"
            and "confirmation_token" not in data4
            and len(ledger_rows()) == before4
        )
        record("4-never-before-authority", ok4,
               f"blocked_by={pol4.get('blocked_by')} token_absent={'confirmation_token' not in data4} card={'yes' if card4 else 'no'} ledger_unchanged={len(ledger_rows()) == before4}")
        rules.save([])

        # -- 5. above-ceiling ALLOW (post) -> still asks; denied -----------
        rules.save([
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"post"})),
                decision=RuleDecision.ALLOW, rule_id="qual-allow-post",
            ),
        ])
        r5, card5 = await flow.begin("post_text", {"text": "m8 qualification probe"})
        pol5 = (r5.data or {}).get("policy", {})
        gate5 = ((r5.data or {}).get("data") or {}).get("rule_gate", {})
        if card5 is not None:
            print(card5.render_text())
            card5.deny()  # nothing is published
        ok5 = bool(
            pol5.get("verdict") == "confirmation_required"
            and card5 is not None
            and gate5.get("ceiling_downgraded") is True
        )
        record("5-above-ceiling-still-asks", ok5,
               f"verdict={pol5.get('verdict')} card={'yes' if card5 else 'no'} ceiling_downgraded={gate5.get('ceiling_downgraded')} matched={gate5.get('matched_rule_id')}")
        rules.save([])

        # -- 6. rule changed between phase 1 and phase 2 --------------------
        r6, card6 = await flow.begin("like_post", {"post_url": posts[1]})
        rules.save([
            UserRule.create(
                selector=RuleSelector(action_types=frozenset({"like"})),
                decision=RuleDecision.NEVER, rule_id="qual-late-never",
            ),
        ])
        if card6 is not None:
            replay = await card6.approve()
            pol6 = (replay.data or {}).get("policy", {})
            human_likes = [r for r in ledger_rows()
                           if r.action_type == "like" and r.approver == "human"]
            ok6 = bool(pol6.get("blocked_by") == "user_rule" and len(human_likes) == 1)
            record("6-rule-changed-phase2", ok6,
                   f"phase2_blocked_by={pol6.get('blocked_by')} human_like_rows={len(human_likes)}")
        else:
            record("6-rule-changed-phase2", False, "phase 1 produced no card")
        rules.save([])

        # -- 7. malformed / reused / caller-supplied ------------------------
        raw_bad = await d.invoke("like_post", {
            "post_url": posts[3], "confirmation_token": "malformed-not-a-token",
        })
        pol7 = (raw_bad.data or {}).get("policy", {})
        ok7a = bool(pol7.get("blocked_by"))
        record("7a-malformed-token", ok7a, f"blocked_by={pol7.get('blocked_by')}")

        raw_p1 = await d.invoke("like_post", {"post_url": posts[1]})
        token1 = ((raw_p1.data or {}).get("data") or {}).get("confirmation_token")
        raw_exec = await d.invoke("like_post", {
            "post_url": posts[1], "confirmation_token": token1,
        })
        pol_exec = (raw_exec.data or {}).get("policy", {})
        raw_replay = await d.invoke("like_post", {
            "post_url": posts[1], "confirmation_token": token1,
        })
        pol7b = (raw_replay.data or {}).get("policy", {})
        ok7b = bool(pol_exec.get("verdict") == "allow"
                    and pol7b.get("blocked_by") in ("consumed_token", "dedupe"))
        record("7b-reused-token-fail-closed", ok7b,
               f"first_use_verdict={pol_exec.get('verdict')} replay_verdict={pol7b.get('verdict')} replay_blocked_by={pol7b.get('blocked_by')}")

        boundary_rejected = False
        try:
            await flow.begin("like_post", {"post_url": posts[1], "confirmation_token": "T"})
        except ValueError:
            boundary_rejected = True
        record("7c-caller-supplied-at-card", boundary_rejected,
               "ValueError raised at CardFlow boundary")

        # -- 8. journal + ledger inspection ---------------------------------
        journal_text = (QUAL_STATE / "journal.ndjson").read_text(encoding="utf-8")
        raw_tokens = []
        for line in journal_text.splitlines():
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            carrier = (rec.get("input_redacted") or {}).get("confirmation_token")
            if carrier is not None and carrier != "<redacted>":
                raw_tokens.append(str(carrier)[:12])
        compact = journal_text.replace(" ", "")
        redacted_present = '"confirmation_token":"<redacted>"' in compact
        all_rows = ledger_rows()
        approvers = [r.approver for r in all_rows]
        expected = {"human", "rule:qual-allow-like"}
        ok8 = bool(not raw_tokens and redacted_present
                   and all(a in expected for a in approvers if a is not None))
        record("8-no-raw-authority-persisted", ok8,
               f"raw_tokens_in_journal={raw_tokens} redacted_marker={redacted_present} ledger={[(r.action_type, r.state.value, r.approver) for r in all_rows]}")

        print("\n=== QUALIFICATION RECORD ===")
        print(json.dumps({
            "environment": env,
            "account": handle,
            "actions": ["like", "post_text(card-only)"],
            "posts_used": posts,
            "results": results,
        }, indent=1, default=str))
        return 0 if all(r["ok"] for r in results) else 1
    finally:
        print("\n=== dispatcher stop ===")
        await d.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
