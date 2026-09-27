# Agent-WebWire

Browser-native X/Twitter capability layer for AI agents — built on the user's
own [Super-Browser](https://github.com/Octo-Lex/Super-Browser) SDK.

**Status: v0.3 — M5 effect-transaction boundary complete; M6 evidence-bearing reconciliation and qualification Layers 1–7 implemented/qualified.** 20 capabilities — 7 read, 13 write — with supported remote mutations routed through scoped M5 authority and the CommitGateway, durable uncertainty governed by the EffectLedger + ReconciliationLedger composite recovery model, both safety-ledger durability paths qualified on actual GitHub-hosted Windows Server 2025 runners for CPython 3.11/3.12, and Layer-7 like/unlike broker/evidence mechanics qualified without making an unsupported replay-policy promotion. Like/unlike deliberately remain `ReplaySemantics.UNKNOWN` / `DurabilityPolicy.REQUIRED` because absence of residual public-engagement side effects has not been established.

## Framing

> "AI proposes, human approves, system enforces authority, browser executes,
> durable effect evidence governs replay; operator-authorized evidence resolves
> durable uncertainty without rewriting history."

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

CI runs the full pytest + Ruff + mypy gate on Ubuntu with Python 3.11/3.12 and a
separate Windows Python 3.11/3.12 durability qualification matrix for the two
safety ledgers and their restart/re-durability boundary.

## Install (editable, local)

```bash
# Super-Browser must be installed first (the SDK dependency).
pip install -e C:/Next-Era/Super-Browser
pip install -e ".[dev]"
```

## Usage — the two-phase confirmation flow

Every supported remote write requires a human-approved, capability-bound,
intent-bound token. A changed capability, target, payload, media, or risk
binding invalidates the confirmation. M6 adds a process-local confirmation
epoch: terminal reconciliation advances that epoch before reconciliation
persistence begins, so confirmation authority minted before reconciliation
cannot cross the recovery boundary afterward.

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

### Local reconciliation workflow

Durable `RESERVED`/`EFFECT_UNKNOWN` history is never rewritten. The supported
local workflow is created from the running `Dispatcher` authority root, stages a
bounded evidence-bearing proposal, requires exact human confirmation, and then
appends one terminal reconciliation fact. A model/evidence collector may inspect
or propose; it cannot commit recovery truth by itself.

```python
session = d.create_reconciliation_operator_session("local-admin")
targets = session.list_targets()
```

A successful terminal reconciliation clears only the matching recovery
uncertainty contribution. It does not restore an old grant/permit/confirmation,
reset dedupe or rate limits, clear the kill switch, or bypass normal policy for a
future invocation.

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
  capability plus immutable intent/risk semantics; it is single-use, monotonic-
  TTL bounded, and epoch-revocable.
- **Registry-gated risk and effect policy.** Unknown actions, risk drift, policy
  drift, actor/target drift, epoch revocation, kill activation, and permit misuse
  fail closed at their respective authority boundaries.
- **Immutable M5 effect history.** `.webwire/effects.ndjson` is the fsync-backed
  EffectLedger. `EFFECT_UNKNOWN` remains terminal historical effect knowledge;
  M6 never changes it into success or no-effect.
- **Orthogonal M6 reconciliation history.** `.webwire/reconciliations.ndjson` is
  a separate fsync-backed append-only safety ledger. `RecoveryProjector` joins
  the two histories, validates exact lineage/evidence, and `RecoveryGuard`
  publishes the composite result under the process-local publication fence.
- **Unknown outcomes are never blindly retried.** Raw `RESERVED` after restart
  and `EFFECT_UNKNOWN` block matching semantic replay until an independently
  authorized terminal reconciliation becomes known durable and published.
- **Durability ambiguity fails closed.** A reconciliation append that may have
  exposed bytes without established durability cannot clear recovery merely
  because the bytes are visible. Exact-fact re-durability or fresh-process
  startup durability establishment is required before the fact is trusted.
- **Windows safety-ledger qualification is bounded.** On the tested GitHub-hosted
  Windows Server 2025 runners, CPython 3.11/3.12 passes the ledger append/fsync,
  exact-fact re-durability, startup re-durability, normalized same-path identity,
  corruption fail-closed, and fresh-process recovery qualification. This is not
  a portable parent-directory-fsync, storage-hardware, network-filesystem, or
  cross-process linearizability claim.
- **Like/unlike qualification is deliberately split from replay-policy truth.**
  The supported live broker/evidence path now fails closed on contradictory,
  missing, hidden, disabled, nested, duplicate, hydrating, or stale directional
  controls; already-satisfied state is zero-mutation; the requested direction is
  revalidated at the exact post-authority click seam; terminal like evidence uses
  the same qualified state reader under the browser lease. None of that proves
  that repeated public engagement has no notification, callback, analytics,
  counter, or other service-side residual effect, so like/unlike remain
  `UNKNOWN` / `REQUIRED` rather than being downgraded to `BEST_EFFORT`.
- **Old authority does not cross reconciliation.** Reconciliation advances the
  confirmation epoch before persistence starts, does not revive M5 grants or
  permits, and restart reconstructs recovery only from durable histories — not
  process-local approval authority.
- **Process-local budgets and semantic dedupe remain independent.** Per-action/
  global token buckets and the TTL dedupe store remain defense-in-depth controls
  while the process is alive. Reconciliation does not refund/reset them.
- **Invocation journal is audit-only.** `.webwire/journal.ndjson` remains
  append-only best-effort diagnostics with rotation/redaction. Its contents
  cannot create, clear, dedupe, rate-limit, approve, reconcile, recover, or
  authorize a mutation.
- **Honest verification.** Evidence is bounded to what it establishes. Missing
  selectors, timeouts, 404s, empty reads, and other absence-like observations do
  not automatically prove `CONFIRMED_NO_EFFECT`.

## Honest limitations

Pre-alpha software driving X's real DOM: selectors can churn; verification is
bounded by what the DOM exposes. Sessions are cookie-persistence only and
`whoami` remains the live actor-identity authority. M5 targets at-most-once
automatic execution for fenced effects; M6 adds explicit evidence-bearing local
reconciliation, not distributed exactly-once semantics. Effect/reconciliation
writer coordination, browser ownership, publication fencing, and confirmation
authority remain supported as **single-process** contracts. M6 does not provide
a remote reconciliation service, automatic model-authorized terminal verdicts,
terminal-verdict correction/supersession, or cryptographic protection against a
hostile local filesystem user. Layer-6 Windows evidence is limited to the tested
GitHub-hosted Windows Server 2025 / CPython safety-ledger environment and does not
establish portable directory-entry, hardware-cache, network-filesystem, or whole
browser-runtime durability. Layer-7 qualifies the tested local broker/evidence
mechanics only; it does not establish platform-side replay safety for public
engagement and therefore does not promote like/unlike from `UNKNOWN` /
`REQUIRED`.

See `docs/M5_DESIGN.md` for the frozen M5 transaction contract,
`docs/M6_DESIGN.md` for the normative reconciliation/qualification contract,
`docs/M6_LAYER6_WINDOWS_QUALIFICATION.md` for the bounded Windows evidence,
`docs/M6_LAYER7_REPLAY_SAFETY_QUALIFICATION.md` for the replay-safety
qualification result and claim ceiling, and `docs/STATE.md` for the living
project record.
