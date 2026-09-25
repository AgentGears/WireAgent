# Agent-WebWire

Browser-native X/Twitter capability layer for AI agents — built on the user's
own [Super-Browser](https://github.com/Octo-Lex/Super-Browser) SDK.

**Status: v0.3 — M5 effect-transaction boundary Layers 1–6 merged; Layer 7 journal audit-only migration in progress.** 20 capabilities — 7 read, 13 write — with supported remote mutations routed through scoped M5 authority and the CommitGateway.

## Framing

> "AI proposes, human approves, system enforces authority, browser executes,
> durable effect evidence governs replay."

Personal, single-user, local-only. Not a product, not multi-tenant.

## Capabilities

| Reads | Writes |
|---|---|
| `whoami` — identity via profile-link href | `bookmark_post` — private, reversible |
| `read` — single post with metrics | `like_post` — directional compensation |
| `read_profile` — fan-out post enumeration | `compose_post` — dry-run preview only |
| `read_thread` — conversation slice | `post_text` / `reply_post` / `quote_post` |
| `read_search` — top/latest/people/media tabs | `post_photo` / `reply_photo` / `quote_photo` |
| `download_image` — separate local-output broker | `post_multi_image` / `reply_multi_image` — ordered manifest transaction |
| `health` — DOM probes, hydration polling, core gating | `quote_multi_image` — quoted target + 2/2 media verified |
|  | `delete_post` — compensation made real; tombstone-verified |

All multi-image writes share one media-compose harness
(`safety/media_compose.py`): preflight-all-before-any-upload, exact-count
verification per attachment, composition re-verification, abort-and-cleanup
on any partial failure, and honest verification (never claims byte
equivalence, rendered order, or unproven attachments).

## Development

```bash
pip install -e ".[dev]"        # offline dev (tests run against the stub SDK)
pip install -e ".[browser]"    # + the Super-Browser SDK for live runs
bash scripts/check.sh           # the gate: pytest + ruff + mypy
```

CI (GitHub Actions, Python 3.11/3.12) runs the same three steps on every push.

## Install (editable, local)

```bash
# Super-Browser must be installed first (the SDK dependency).
pip install -e C:/Next-Era/Super-Browser
pip install -e ".[dev]"
```

## Usage — the two-phase confirmation flow

Every supported remote write requires a human-approved, capability-bound,
intent-bound token. A changed capability, target, payload, media, or risk
binding invalidates the confirmation.

```python
import asyncio
from webwire import Dispatcher

async def main():
    d = Dispatcher()
    await d.start()

    who = await d.invoke("whoami")               # binds actor identity
    print(who.data)

    # Phase 1: preview + confirmation token (no mutation).
    p1 = await d.invoke(
        "bookmark_post",
        {"post_url": "https://x.com/a/status/123"},
    )
    print(p1.data["data"]["preview"])

    # Phase 2: same capability + same approved intent.
    p2 = await d.invoke(
        "bookmark_post",
        {
            "post_url": "https://x.com/a/status/123",
            "confirmation_token": p1.data["data"]["confirmation_token"],
        },
    )
    print(p2.data["trace"]["stages"])

    await d.stop()

asyncio.run(main())
```

### Kill switch

```python
d.kill_switch.trip()   # in-process flag + .webwire/kill hot file
```

Refuses further capability execution at the Dispatcher boundary and at M5
commit-authority boundaries; leaves the browser intact. External trip: create
`.webwire/kill`.

## Safety model

- **Capability contracts, not commands.** Read capabilities receive bounded read
  surfaces; supported mutations cross scoped M5 authorities rather than a raw
  browser mutation surface.
- **Capability- and intent-bound confirmation.** Confirmation authority binds the
  capability plus immutable intent/risk semantics; it is single-use and
  expiring.
- **Registry-gated risk and effect policy.** Unknown actions, risk drift, policy
  drift, actor/target drift, epoch revocation, kill activation, and permit misuse
  fail closed at their respective authority boundaries.
- **Durable uncertain-effect replay safety.** `.webwire/effects.ndjson` is the
  fsync-backed M5 EffectLedger. `RecoveryGuard` hydrates unresolved `RESERVED`
  and `EFFECT_UNKNOWN` semantic keys before browser startup and refreshes before
  supported M5 writes. Matching automatic replay is denied with reconciliation
  required.
- **Process-local budgets and semantic dedupe.** Per-action/global token buckets
  and the TTL dedupe store remain defense-in-depth controls while the process is
  alive. Layer 7 intentionally stops rebuilding them from the best-effort audit
  journal; process restart resets those windows. A future cross-restart budget
  requirement needs its own durable policy-state contract.
- **Unknown outcomes are never blindly retried.** Once an external effect is
  durably uncertain, RecoveryGuard—not a best-effort journal row—owns the
  restart denial.
- **Invocation journal is audit-only.** `.webwire/journal.ndjson` remains
  append-only best-effort diagnostics with rotation/redaction. Its write facts
  (`action_type`, `risk_tier`, `dedupe_key`) are evidence only and do not create
  commit, recovery, dedupe, or rate-limit authority.
- **Honest verification.** Confirmation is evidence-bound; ambiguous readback is
  represented as uncertainty rather than inferred success.

## Honest limitations

Pre-alpha software driving X's real DOM: selectors can churn; verification is
bounded by what the DOM exposes. Sessions are cookie-persistence only and
`whoami` remains the live actor-identity authority. M5 targets at-most-once
automatic execution for fenced effects plus explicit reconciliation after
uncertainty; it does not claim distributed exactly-once semantics. The current
ledger/browser coordination is single-process, and external hot-file kill
activation is re-observed at final authority boundaries rather than claimed as
strict cross-process linearization.

See `docs/M5_DESIGN.md` for the frozen M5 contract,
`docs/M5_LAYER7_PLAN.md` for the final migration boundary, and `docs/STATE.md`
for the living project record.
