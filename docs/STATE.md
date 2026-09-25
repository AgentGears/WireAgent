# Agent-WebWire — Living Project State

> Single source of truth for current project state. Update on every meaningful
> change. Detailed historical state through the Layer-3 close-out is preserved
> verbatim in `docs/STATE_HISTORY_THROUGH_2026-09-24.md`; new history is appended
> at the bottom of this file.

## Identity

- **Repo:** `AgentGears/WireAgent` (local working home historically
  `C:\Next-Era\Agent-WebWire`).
- **What it is:** Personal, single-user, local-only browser-native X/Twitter
  capability layer for AI agents, built on the user's Super-Browser SDK.
- **Framing:** AI proposes, human approves, scoped authority executes, durable
  effect evidence governs replay. The invocation journal is audit evidence, not
  mutation authority.

## Current version

**v0.3 stabilized live path + M5 effect-transaction boundary complete.**

Canonical merged baseline after the full M5 build order:

```text
main = 0c62402ae01b50d7662b3978cbf2bee4109aa035
```

M5 status:

```text
1. EffectPolicy + EffectLedger                 MERGED
2. ApprovalGrant / EffectAttempt               MERGED
3. CommitGateway + EffectPermit                MERGED
4. Scoped authorities                          MERGED
5. Capability migration                        MERGED  PR #6
6. RecoveryGuard                               MERGED  PR #7
7. Journal audit-only live role                MERGED  PR #8
```

Normative M5 contract: `docs/M5_DESIGN.md`. Final Layer-7 boundary and evidence:
`docs/M5_LAYER7_PLAN.md`.

## Architecture invariants — do not violate

1. **Owned browser is the production path.** Attach mode is diagnostic and must
   not become implicit authority.
2. **Capability contracts, not raw commands.** Capability-facing code does not
   receive the raw SuperBrowser mutation facade.
3. **Supported remote writes cross M5 scoped authority and one CommitGateway.**
   Unmigrated WRITE capabilities fail closed; `compose_post` is a deliberate
   no-effect dry-run shell.
4. **Human approval and execution authority are separate.** Confirmation tokens
   are capability-bound, intent-bound, risk-bound, expiring, and single-use;
   M5 ApprovalGrant/EffectPermit authority is independently fenced.
5. **Actor identity is live authority.** Cookie persistence is convenience only;
   supported writes require a whoami-resolved actor for the current run.
6. **Kill dominates execution.** In-process trip is ordered with commit
   authority through the kill/epoch fences. External hot-file activation is
   re-observed at final authority boundaries without claiming impossible
   cross-process linearization.
7. **Risk, semantic authority, replay behavior, and durability are independent.**
   SAFE replay classifications require concrete broker-level evidence.
8. **Writes are semantic and directional.** State-set operations must not
   collapse into unsafe toggles. Bookmark/remove-bookmark are proven directional;
   like/unlike remain conservative `UNKNOWN`/`REQUIRED` until equivalent broker
   evidence exists.
9. **One M5 attempt owns one stable effect identity.** Ambiguous durability retry
   re-addresses the same fact; reservation-start latching forbids false clean
   release after durable I/O may have begun.
10. **Immutable lineage crosses blocking commit work.** Caller-owned mutable
    `WriteIntent` is snapshotted once; actor, target, action, semantic key,
    intent hash, policy binding, and epoch remain bound.
11. **No REQUIRED mutation authority before durable reservation.** Durable
    `RESERVED` precedes permit exposure; terminal durable outcome precedes
    in-memory terminalization/eviction.
12. **Approval spend is final-boundary validated.** Grant liveness/bindings/epoch
    and `ACTIVE -> SPENT` are one grant-lock transition; kill is re-observed and
    permit time sampled afterward.
13. **Grant and permit TTLs are monotonic elapsed-time authority.** Ledger
    timestamps remain UTC wall-clock provenance; clock domains are never
    directly compared.
14. **Composer/read coordination shares one browser-state lease.** Read/evidence
    navigation cannot race an owned content composer in the supported process.
15. **Evidence, not attempted mutation, determines terminal truth.** Confirmed
    evidence records `EFFECT_CONFIRMED`; ambiguous/post-authority outcomes become
    `EFFECT_UNKNOWN` and require reconciliation.
16. **Never blindly replay durable uncertainty.** RecoveryGuard hydrates raw
    `RESERVED`/`EFFECT_UNKNOWN` semantic keys before browser startup and refreshes
    before supported M5 write policy passes. Matching automatic replay is denied
    before preview/browser interaction.
17. **EffectLedger is the durable M5 safety authority.** Schema/history/lineage
    corruption fails closed. Exact-fact retry may re-fsync but may not rewrite
    contradictory history.
18. **Invocation journal is audit-only.** Journal content must not create, clear,
    dedupe, rate-limit, approve, recover, or authorize a mutation.
19. **Dedupe TTL and token-bucket budgets are process-local defense in depth.**
    Restart resets those in-memory windows. A future cross-restart budget/dedupe
    requirement needs a dedicated durable policy store, not best-effort audit
    data.
20. **Same-process least authority is not a hostile-code sandbox.** Untrusted
    extensions require stronger process/OS isolation and no raw-browser escape.

## M5 — completed transaction boundary

The supported remote-write path is:

```text
Dispatcher
  -> WriteKernel confirmation/policy shell
  -> RecoveryGuard exact semantic replay gate
  -> M5 capability adapter
  -> scoped preparation/effect authority
  -> CommitGateway
  -> durable reservation when REQUIRED
  -> single-use EffectPermit consume at canonical mutation seam
  -> external effect
  -> evidence-bound EFFECT_CONFIRMED | EFFECT_UNKNOWN
  -> EffectLedger
```

Key current facts:

- `EffectPolicy` owns replay/durability truth and effect scope.
- `ApprovalGrant` is deliberately ephemeral; durable effect knowledge belongs to
  EffectLedger.
- `CommitGateway` performs final kill/policy/epoch/grant/TTL validation and owns
  permit lifecycle/outcome recording.
- Layer 4 scoped authorities expose only the approved semantic mutation surface.
- Layer 5 migrated bookmark/like, text, reply, quote, media, and delete writes
  through that boundary and disabled supported legacy mutation fallback.
- Layer 6 RecoveryGuard turns unresolved durable effect facts into enforced
  pre-browser semantic replay denial; it refreshes within the same process as
  well as at startup.
- Layer 7 removes the last journal-to-safety-state data path. The journal remains
  best-effort invocation evidence with redaction/rotation.

## Phase plan

| Phase | Scope | Status |
|---|---|---|
| 0a | session + whoami + health + envelope + journal + kill switch + reads | LIVE-VERIFIED |
| 0b | confirmation/risk/budget/dedupe WriteKernel shell | DONE |
| 1–4d | core reads + bookmark/like + text/reply/quote | LIVE-VERIFIED / DONE |
| v0.2 M1–M4c | media family + download + multi-image | DONE / LIVE-VERIFIED where recorded |
| M5 L1 | EffectPolicy + durable EffectLedger | MERGED |
| M5 L2 | ApprovalGrant + EffectAttempt | MERGED |
| M5 L3 | CommitGateway + EffectPermit | MERGED |
| M5 L4 | scoped authorities | MERGED |
| M5 L5 | concrete capability migration | MERGED — PR #6 |
| M5 L6 | RecoveryGuard startup/refresh enforcement | MERGED — PR #7 |
| M5 L7 | invocation journal becomes audit-only | MERGED — PR #8 |

## Capabilities

Supported capability inventory remains 20 total:

- Reads: `whoami`, `health`, `read`, `read_profile`, `read_thread`,
  `read_search`, `download_image`.
- Writes/no-effect shell: `bookmark_post`, `like_post`, `compose_post`,
  `post_text`, `reply_post`, `quote_post`, `post_photo`, `reply_photo`,
  `quote_photo`, `post_multi_image`, `reply_multi_image`, `quote_multi_image`,
  `delete_post`.

`compose_post` is a dry-run shell and does not cross an external mutation seam.
Supported remote mutations use the M5 adapters/scoped authority stack.

## Safety / persistence surfaces

| Surface | Role | Durability / authority |
|---|---|---|
| `.webwire/effects.ndjson` | M5 effect facts | fsync-backed; authoritative; fail-closed |
| `RecoveryGuard` | unresolved semantic replay denial | derived from EffectLedger; enforcement |
| ApprovalGrant / EffectPermit | process-local approval/execution authority | ephemeral, fenced, monotonic TTL |
| DedupeStore | repeated semantic-write suppression | process-local only |
| TokenBucket | per-action/global circuit breaker | process-local only |
| `.webwire/journal.ndjson` | invocation audit / diagnostics | best-effort output only |
| `.webwire/session.json` | cookie/session convenience | never actor authority |

## Broker / authority surfaces

| Surface | Purpose |
|---|---|
| `ReadOnlyBroker` / leased read broker | coordinated browser reads/evidence |
| `DownloadBroker` | bounded local filesystem output |
| scoped M5 authorities | approved semantic mutation ports |
| M5 scoped/live write broker internals | canonical permit-consume mutation seams |
| legacy raw WriteBroker path | unavailable from supported Dispatcher migration path |

## Module map — selected

| File | Role |
|---|---|
| `dispatcher.py` | invocation entry + M5 live routing |
| `journal.py` | best-effort audit-only NDJSON journal |
| `safety/write_kernel.py` | confirmation/policy shell + recovery gate integration |
| `safety/effect_policy.py` | replay/durability/effect-scope policy |
| `safety/effect_ledger.py` | durable effect safety ledger |
| `safety/execution_models.py` | ApprovalGrant / EffectAttempt state machines |
| `safety/commit_gateway.py` | EffectPermit mint/consume/outcome boundary |
| `safety/scoped_authority.py` | least-authority semantic ports |
| `safety/recovery_guard.py` | restart + same-process unresolved replay enforcement |
| `safety/m5_live_runtime.py` | coherent live M5 execution stack |
| `safety/dedupe.py` | process-local semantic dedupe |
| `safety/token_bucket.py` | process-local write circuit breaker |
| `safety/kill_switch.py` | generation-based kill + critical revocation fence |

## Known gaps / open items

- [ ] **Reconciliation semantics:** `EFFECT_UNKNOWN` remains terminal historical
  evidence; future resolution needs explicit evidence-bearing semantics rather
  than overwrite.
- [ ] **Cross-process EffectLedger/browser coordination:** current safety contract
  is single-process.
- [ ] **External kill-file strict atomicity:** final-boundary re-observation is
  implemented; a non-cooperating external writer cannot share the Python lock.
- [ ] **Windows durability runner evidence:** behavior is modeled/tested but CI
  currently runs Ubuntu.
- [ ] **Like/unlike BEST_EFFORT proof:** remain `UNKNOWN`/`REQUIRED` pending
  broker-level state-preserving evidence comparable to bookmark.
- [ ] reply_photo URL-capture gap.
- [ ] Phase 1b edge cases need real fixtures.
- [ ] Screenshot capture is plumbed but not implemented.
- [ ] No CLI yet.
- [ ] Cookie-only persistence remains fragile.
- [ ] Profile display_name extraction remains unreliable.
- [ ] Future follow/unfollow and analytics work.

## Test fixtures

- Current target post: `https://x.com/infaag/status/2102451358305771541`.
- Test images: `.webwire/test-media/test_red.png`,
  `.webwire/test-media/test_blue.png`.
- Session: `.webwire/session.json`; auth validity is re-established by `whoami`.

## History

- **2026-09-25 — M5 complete; L7 merged (PR #8).** Final Layer-7 candidate
  `390967d96f87cdb483334bd8a2120629312df3f4`; CI #366 green on Python
  3.11/3.12 with **765 tests** on Python 3.11, Ruff clean, and mypy clean across
  78 source files. Codex exact-head review was unavailable because the repository
  code-review usage limit was exhausted; no GitWire review appeared. Per the
  standing review rule, a distinct maintainer adversarial second pass substituted
  for the unavailable external reviewer and found one additional coverage defect:
  journal rotation regression coverage had been lost when the legacy hydration
  suite was retired. The final candidate restored explicit rotation, URL
  redaction, screenshot-policy, and real journal-I/O-failure regressions and was
  revalidated by exact-head CI. Squash merge:
  `0c62402ae01b50d7662b3978cbf2bee4109aa035`. **M5 Layers 1–7 are complete.**
- **2026-09-25 — M5 L7 maintainer-first candidate work.** Started from merged
  Layer-6 baseline `03df4d3458828cc131989fd44fe27d48cef1240c`. Frozen Layer-7
  boundary: journal audit/diagnostics only; EffectLedger + RecoveryGuard remain
  durable unresolved replay authority; DedupeStore and TokenBucket are explicit
  process-local defense-in-depth controls. Maintainer-first review found and
  fixed: a dormant Dispatcher journal-hydration coupling; a misleading silent
  compatibility reader; stale public README restart-budget claims; stale audit
  `policy_decision="allowed"` labeling; and obsolete hydration APIs/tests.
- **2026-09-25 — M5 L6 merged (PR #7).** Candidate
  `f4e9f7c88a6c6f6fe64abbbd899352bd12eafeeb`; CI #363 green on Python
  3.11/3.12 with 763 tests, Ruff and mypy. Exact-head GitWire review completed
  with zero findings. Maintainer-first review had already discovered/fixed three
  authority defects: stale concurrent RecoveryGuard publication, caller-
  controllable recovery exemption, and cross-capability confirmation-token
  transfer. Squash merge: `03df4d3458828cc131989fd44fe27d48cef1240c`.
- **2026-09-25 — M5 L5 merged (PR #6).** Final candidate
  `7885de6abd385c7da25b0ac61d61627d2f465c54`; CI #353 green on Python
  3.11/3.12 with 749 tests, Ruff and mypy. Exact-head GitWire coverage was
  incomplete because of its bundle line limit; its three findings were
  independently reconciled. Exact-head Codex rerun was unavailable because the
  repository review quota was exhausted. Squash merge:
  `515999740bb7b5fa4ad9c74582d6166157e9075b`.
- **2026-09-25 — M5 L4 already merged.** Scoped-authority baseline merged as
  `5650982b718f78c8aa10709cdb9f257e73374ce2`; Layer 5 was built from that
  exact main baseline.
- Detailed project history through the Layer-3 close-out on 2026-09-24 is
  preserved verbatim in `docs/STATE_HISTORY_THROUGH_2026-09-24.md`.
