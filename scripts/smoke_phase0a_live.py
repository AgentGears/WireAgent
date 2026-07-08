"""Phase 0a live smoke test — runs against the real logged-in Chrome.

Implements ChatGPT's Q1 acceptance gate (6 items):
1. SessionManager.start() attaches via PATCHRIGHT_ATTACH
2. health runs on a real X surface and returns a meaningful diagnostic
3. whoami resolves the authenticated account
4. Kill file trips mid-session and blocks the next path
5. Journal records emitted for success / killed / unsupported
6. URL redaction confirmed with a real target

Run: python scripts/smoke_phase0a_live.py
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Make 'webwire' importable when running from repo root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from webwire import Dispatcher, WebWireConfig


def _print(label: str, payload) -> None:
    import copy
    print(f"\n=== {label} ===")
    if hasattr(payload, "to_dict"):
        d = copy.deepcopy(payload.to_dict())  # deepcopy so we don't mutate the live result
        # Truncate large data blobs for readability.
        if isinstance(d.get("data"), dict):
            for k, v in list(d["data"].items()):
                if isinstance(v, (list, dict)) and len(str(v)) > 200:
                    d["data"][k] = f"<{len(v) if hasattr(v,'__len__') else '?'} items>"
        print(json.dumps(d, indent=2, default=str)[:1500])
    else:
        print(payload)


async def main() -> int:
    cfg = WebWireConfig(state_dir=Path(".webwire"))
    d = Dispatcher(cfg)

    failures: list[str] = []

    # ---- Gate 1: start (attach to real Chrome) ----
    print("=== GATE 1: SessionManager.start() (PATCHRIGHT_ATTACH) ===")
    start_result = await d.start()
    _print("start result", start_result)
    if not start_result.ok:
        failures.append("GATE 1 start failed — cannot proceed with remaining gates.")
        _summarize(cfg, failures)
        return 1
    print("GATE 1: PASS — attached to real Chrome")

    try:
        # ---- Gate 3: whoami (run before health; identity is foundational) ----
        print("\n=== GATE 3: whoami (resolve authenticated account) ===")
        who = await d.invoke("whoami")
        _print("whoami result", who)
        if not who.ok:
            failures.append(
                f"GATE 3 whoami failed (ok=False). "
                f"This is a HARD BLOCKER for Phase 1 — identity is foundational. "
                f"See identity_unresolved_on_authenticated_surface in the result."
            )
        else:
            data = who.data or {}
            if not data.get("handle") or not data.get("profile_url"):
                failures.append("GATE 3 whoami ok but incomplete identity (missing handle/profile_url).")
            else:
                print(f"GATE 3: PASS — handle=@{data.get('handle')}, source={data.get('source')}")

        # ---- Gate 2: health ----
        print("\n=== GATE 2: health (diagnostic on real X) ===")
        h = await d.invoke("health")
        _print("health result", h)
        if not h.ok:
            # health may return AUTH_REQUIRED (ok=False) — that's meaningful data, not a smoke failure,
            # UNLESS it's not auth-related.
            fc = h.failure_category.value if h.failure_category else None
            if fc == "auth_required":
                print("GATE 2: NOTE — health returned AUTH_REQUIRED (login wall). Meaningful diagnostic, but blocks reads.")
                failures.append("GATE 2 health hit a login wall — session may not actually be authenticated on X.")
            else:
                failures.append(f"GATE 2 health failed (failure_category={fc}).")
        else:
            diag = h.data or {}
            checks = diag.get("checks", {})
            ready = diag.get("ready")
            print(f"GATE 2: {'PASS' if ready else 'NOTE'} — ready={ready}, checks={list(checks.keys())}")
            if not ready:
                failures.append("GATE 2 health ok=False or ready=False — see diagnostic probes.")

        # ---- Gate 4: kill switch mid-session ----
        print("\n=== GATE 4: kill switch trips and blocks next invocation ===")
        d.kill_switch.trip()
        blocked = await d.invoke("whoami")  # a cap that would otherwise work
        _print("killed whoami result", blocked)
        if blocked.ok or (blocked.failure_category.value != "security"):
            failures.append("GATE 4 kill switch did NOT block a post-trip invocation.")
        else:
            # Verify the hot file exists (external trip mechanism).
            hot = cfg.kill_path()
            print(f"GATE 4: PASS — blocked ({blocked.failure_category.value}); hot file exists={hot.exists()}")
        d.kill_switch.reset()

        # ---- Gate 5: journal records (success / killed / unsupported) ----
        # We already produced: whoami(success?), health(success?), whoami(killed).
        # Add an unsupported to round out the set, then inspect the journal.
        print("\n=== GATE 5: journal records for success/killed/unsupported ===")
        unsup = await d.invoke("like", {"post_url": "https://x.com/test/status/999?secret=abc"})
        _print("unsupported result", unsup)

        jpath = cfg.journal_path()
        if not jpath.exists():
            failures.append("GATE 5 journal file not created.")
        else:
            lines = [ln for ln in jpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
            print(f"journal records: {len(lines)}")
            decisions_seen: set[str] = set()
            for ln in lines:
                rec = json.loads(ln)
                decisions_seen.add(rec.get("policy_decision"))
                print(f"  - {rec['capability']:8} | {rec.get('policy_decision'):11} | "
                      f"ok={rec.get('result_ok')} | fc={rec.get('failure_category')}")
            for expected in ("allowed", "killed", "unsupported"):
                if expected not in decisions_seen:
                    failures.append(f"GATE 5 missing journal policy_decision={expected!r}")
            if decisions_seen >= {"allowed", "killed", "unsupported"}:
                print("GATE 5: PASS — all three decision types present")

        # ---- Gate 6: URL redaction with a real target ----
        print("\n=== GATE 6: URL redaction (query stripped from target) ===")
        # The 'like' invocation above carried ?secret=abc. Check the journal record.
        if jpath.exists():
            lines = [ln for ln in jpath.read_text(encoding="utf-8").splitlines() if ln.strip()]
            like_rec = None
            for ln in lines:
                rec = json.loads(ln)
                if rec.get("capability") == "like":
                    like_rec = rec
                    break
            if like_rec is None:
                failures.append("GATE 6 no 'like' journal record found to check redaction.")
            else:
                tgt = like_rec.get("target", "")
                journal_raw = jpath.read_text(encoding="utf-8")
                if "secret" in journal_raw or "abc" in journal_raw:
                    failures.append(f"GATE 6 redaction FAILED — secret/query present in journal. target={tgt!r}")
                elif tgt == "https://x.com/test/status/999":
                    print(f"GATE 6: PASS — target redacted to {tgt!r}, no query leaked")
                else:
                    failures.append(f"GATE 6 redaction unexpected: target={tgt!r}")

    finally:
        print("\n=== stopping session ===")
        stop_result = await d.stop()
        if not stop_result.ok:
            print(f"WARNING: stop() returned ok=False: {stop_result.error}")

    _summarize(cfg, failures)
    return 1 if failures else 0


def _summarize(cfg, failures: list[str]) -> None:
    print("\n" + "=" * 60)
    if failures:
        print(f"SMOKE RESULT: FAIL — {len(failures)} gate failure(s):")
        for i, f in enumerate(failures, 1):
            print(f"  {i}. {f}")
    else:
        print("SMOKE RESULT: PASS — all 6 gates satisfied.")
    print("=" * 60)


if __name__ == "__main__":
    rc = asyncio.run(main())
    sys.exit(rc)
