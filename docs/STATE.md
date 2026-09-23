# Agent-WebWire — Living Project State

> Single source of truth for project state, consulted at the start of every
> ChatGPT engagement to defeat conversation fragmentation. Update on every
> meaningful change. Append-only history at the bottom.

## Identity

- **Repo:** `C:\Next-Era\Agent-WebWire` (local, git-initialized)
- **Bound ChatGPT project:** `g-p-6a46471ac4308191bedb18f9a3562c95` ("Agent-WebWire")
- **Active conversation:** see `.chatgpt-collaboration.json` (binding file)
- **What it is:** Personal, single-user, local-only, browser-native X/Twitter
  capability layer for AI agents. Built on the user's own Super-Browser SDK.
  Not a product, not multi-tenant.
- **Framing:** "AI proposes, human approves, system enforces budgets, browser
  executes, journal proves." The M5 migration refines the final clause for
  external-effect safety: the durable EffectLedger, not the invocation journal,
  is the M5 authority for facts it records.

## Current version

**v0.3 stabilized live path + M5 layer-3 candidate** — 20 live/implemented
capabilities; **469 tests** on the reviewed M5 branch; CI-enforced on Python
3.11/3.12; Ruff clean; mypy clean across 51 source files.

M5 status:

```text
1. EffectPolicy + EffectLedger                 DONE
2. ApprovalGrant / EffectAttempt               DONE
3. Commit Gateway                              CANDIDATE — independently reviewed / green
4. Scoped authorities                          NEXT
5. Capability migration                        pending
6. RecoveryGuard                               pending
7. Journal becomes audit-only in live runtime  pending
```

Normative M5 contract: `docs/M5_DESIGN.md`.

The existing concrete capabilities still execute through the legacy WriteKernel
path. Layer 3 therefore establishes the new transaction boundary primitives but
does **not** yet claim full-path enforcement. The invocation journal retains its
legacy live-runtime hydration role until layers 5–7 migrate execution and
RecoveryGuard onto M5.

## Architecture invariants (do not violate)

1. **Agent-WebWire owns its browser by default.** Launch (owned) is the golden
   path. Attach mode is a non-production diagnostic path, refused unless
   `allow_attach=True`. Never attach to a foreign browser.
2. **Capability contracts, not commands.** Capabilities receive broker surfaces,
   never the raw `SuperBrowser` facade.
3. **Write capabilities are physically absent until the safety kernel allows
   them.** Registry tier-gates; WRITE registration is allowed but current live
   routing remains through the WriteKernel pipeline until M5 layer 5.
4. **Kill switch is checked at dispatcher top AND mutation boundaries.** Kill
   dominates unsupported resolution. M5 strictly linearizes in-process trip
   activation with commit authority and generation-based epoch revocation.
   External hot-file activation is re-observed at final mint/consume boundaries
   but is not falsely claimed to be a cross-process atomic transition.
5. **Persistence is a convenience, not an auth authority source.**
   `load_session` success means only cookies loaded. `whoami` is the real auth
   gate. Never save a logged-out jar over a known-good file.
6. **Envelope reuse, not rebuild.** Reuse Super-Browser's `ActionResult` plus the
   `FailureCategory` taxonomy; extend only at the registry boundary.
7. **Verify resolved values, not just ok=True.** This has caught real production
   bugs repeatedly; it remains project law.
8. **Writes are semantic, not toggle-based.** `click_like()` ≠ `click_unlike()`;
   bookmark/remove-bookmark are directional state-setting operations. A
   replay-safety policy claim is made only when the concrete broker behavior is
   regression-proven; like/unlike therefore remain M5 `UNKNOWN`/`REQUIRED` for
   now even though the higher-level capability performs a pre-state read.
9. **Compensation reverses only THIS invocation's delta.** Pre-existing state →
   `already_satisfied` no-op, no compensation.
10. **Public content writes require frozen intent.** Legacy path binds normalized
    text/media into the confirmation token. M5 additionally captures one private
    immutable intent snapshot for the full commit sequence and never re-reads
    the caller-owned mutable `WriteIntent` across blocking reservation I/O.
    Commit authority also requires non-empty target type/id so the lineage is
    representable by the durable EffectLedger schema.
11. **Composer DOM read-back before submit.** The last pre-submit assertion
    proves what the browser is about to submit.
12. **Final kill switch before irreversible submit.** "Hand on the button."
13. **One invalid manifest item rejects the entire invocation.** No partial
    uploads, no partial posts. Any failure before submit → abort-and-cleanup.
14. **Verification records the basis of proof.** When DOM evidence is
    unavailable, record the actual evidence basis honestly.
15. **Legacy live-path restart safety is journal-hydrated until M5 migration is
    complete.** Dedupe keys and token-bucket budgets rebuild from journaled
    WriteKernel facts. This is a statement about the currently integrated path,
    not M5 commit authority. Missing/corrupt journal still fails open for those
    memory stores; confirmation itself does not depend on the journal.
16. **M5 durable effect facts live in `effects.ndjson`, not the invocation
    journal.** REQUIRED reservations and successors are fsync-backed and
    fail-closed; surviving BEST_EFFORT confirmed/unknown outcomes are also
    recorded. Contradictory ledger history is corruption, not “last row wins.”
17. **One M5 attempt owns one stable effect identity.** Ambiguous fsync retry
    targets the same fact; REQUIRED reservation start is latched before I/O and
    forbids generic clean release afterward.
18. **Approval, intent, epoch, and permit time are distinct authority concepts.**
    Grant expiry and permit TTL are process-local elapsed-time authority and
    default to monotonic clocks. In the final mint sequence the permit clock is
    sampled after reservation work, then `spend_if_live()` refreshes grant expiry
    and validates bindings/epoch while performing `ACTIVE → SPENT` under one
    grant lock; no gateway-clock call separates final approval validation from
    spend. Permit expiry is sampled again at the actual consume transition after
    blocking policy/epoch/kill checks. Grant and gateway clock values are never
    compared or claimed simultaneous. Direct authorization-epoch changes are
    fenced with final mint/consume; kill validity is rechecked at final authority
    boundaries. Durable ledger timestamps remain UTC wall-clock provenance.
19. **Never blindly replay explicit uncertainty.** Durable `RESERVED` or
    `EFFECT_UNKNOWN` requires reconciliation once RecoveryGuard is integrated.
20. **Same-process least authority is not a hostile-code sandbox.** Untrusted
    adapters require stronger process/OS isolation and no raw-browser escape.

## M5 — Effect Transaction Boundary

The full contract lives in **docs/M5_DESIGN.md** — normative and
self-contained; build from it, not from this section.

M5 arose from a multi-document adversarial review loop and was then refined by
implementation/fault evidence. The governing change rule remains: revise the
normative design only when implementation, fault injection, live evidence, or
an independently verified review finding falsifies an assumption.

Key current layer-3 facts:

- `EffectPolicy` separates impact, semantic authority, replay semantics, and
  durability. `SAFE_*` is positive evidence, not a convenience label:
  bookmark/remove-bookmark are `SAFE_STATE_SET`/BEST_EFFORT; like/unlike remain
  `UNKNOWN`/REQUIRED until concrete broker-level replay safety is proven.
- `ApprovalGrant` is intentionally ephemeral; durable safety state belongs to
  the EffectLedger, not persisted confirmation tokens. Final approval spend uses
  `spend_if_live()` so grant-clock expiry/binding validation and
  `ACTIVE → SPENT` are one grant-lock transition.
- `EffectAttempt` owns stable `attempt_id`, stable `effect_id`, and the monotonic
  `reservation_started` latch.
- `CommitGateway` authorization lock order is protocol → kill fence → policy
  fence → grant claim fence, with the authorization-epoch fence held only at the
  final authority transition. Permit consumption uses protocol → kill → policy
  → epoch.
- Every permit target is validated as non-empty/persistable before either a
  REQUIRED reservation or a BEST_EFFORT permit can be created.
- REQUIRED: snapshot → validate target/bindings → latch → durable `RESERVED` →
  fenced epoch → final permit-clock sample → callback-free kill refresh → atomic
  grant `spend_if_live()` → mint.
- BEST_EFFORT: only proven replay-safe semantics may omit precommit reservation;
  final permit-clock sample, kill/epoch check, and atomic grant spend still
  precede mint.
- process-local approval/permit TTL defaults use `time.monotonic()`; independent
  grant/permit clock values are never compared. Permit time is sampled in the
  final mint sequence and expiry is re-sampled immediately before consumption
  after blocking authority checks. Ledger timestamps remain UTC wall time.
- in-process `trip()` is process-local lock-linearized with authority crossing;
  external hot-file creation is callback-free re-observed at final mint/consume
  but cannot be made strictly cross-process atomic without a cooperating lock
  protocol.
- `EffectLedger` validates strict schema, immutable lineage, monotonic states,
  and same-path process-local writer serialization.
- Exact same-fact retry re-fsyncs a visible-but-ambiguously-durable row instead
  of appending a duplicate.
- If a durable fenced reservation exists but approval/epoch/kill becomes invalid
  before permit mint, the gateway records `NO_EFFECT`; if that close fails, the
  raw `RESERVED` remains unresolved and no authority is minted.
- Layer 4+ remains required before this boundary governs concrete browser writes.

## Phase plan

| Phase | Scope | Status |
|-------|-------|--------|
| **0a** | session + whoami + health + envelope + journal + kill switch + read-only broker | **LIVE-VERIFIED** |
| **0b** | write-safety kernel (token-bound confirmation, risk, limits, dedupe) | **DONE** |
| **1** | golden read (`read <post_url>`) | **LIVE-VERIFIED** |
| **1b** | quote-tweet impl, display_name fix, unavailable-post handling | **DONE** |
| **2** | read_profile (fan-out) | **LIVE-VERIFIED** |
| **2b** | read_thread (conversation slice) | **LIVE-VERIFIED** |
| **2c** | read_search | **LIVE-VERIFIED** |
| **3** | bookmark_post | **LIVE-VERIFIED** |
| **3b** | like_post | **LIVE-VERIFIED** |
| **4a** | compose_post (dry-run only) | **VERIFIED** |
| **4b** | post_text | **LIVE-VERIFIED** |
| **4c** | reply_post | **LIVE-VERIFIED** |
| **4c-v** | reply verification patch | **LIVE-VERIFIED** |
| **4d** | quote_post | **LIVE-VERIFIED** |
| **4d-v** | quote attachment verification patch | **LIVE-VERIFIED** |
| **5** | analytics (separate adapter family) | not started |
| **v0.2 M1** | post_photo | **LIVE-VERIFIED** |
| **v0.2 M2** | download_image | **LIVE-VERIFIED** |
| **v0.2 M3a** | reply_photo | **DONE** (verification gap flagged) |
| **v0.2 M3b** | quote_photo | **LIVE-VERIFIED** |
| **v0.2 M4a** | post_multi_image | **LIVE-VERIFIED** + runtime tests |
| **v0.2 M4b** | reply_multi_image | **LIVE-VERIFIED** + runtime tests |
| **v0.2 M4c** | quote_multi_image | **LIVE-VERIFIED** |
| **M5 L1** | EffectPolicy + durable EffectLedger | **DONE** |
| **M5 L2** | ApprovalGrant + EffectAttempt | **DONE** |
| **M5 L3** | CommitGateway + EffectPermit | **CANDIDATE — 469 green / independently reviewed** |
| **M5 L4** | scoped broker authorities | **NEXT** |
| **M5 L5** | concrete capability migration | pending |
| **M5 L6** | RecoveryGuard | pending |
| **M5 L7** | journal audit-only live role | pending |

## Capabilities (20)

| Capability | Tier | Status | Notes |
|-----------|------|--------|-------|
| whoami | read | LIVE-VERIFIED | resolved handle binds actor identity |
| read | read | LIVE-VERIFIED | single post |
| read_profile | read | LIVE-VERIFIED | profile fan-out + retweet provenance |
| read_thread | read | LIVE-VERIFIED | target/ancestors/replies |
| read_search | read | LIVE-VERIFIED | X search tabs |
| download_image | read | LIVE-VERIFIED | separate DownloadBroker |
| health | read | LIVE-VERIFIED | readiness/selector diagnostics |
| bookmark_post | write | LIVE-VERIFIED | directional private state-set |
| like_post | write | LIVE-VERIFIED | directional engagement + compensation |
| compose_post | write | VERIFIED | dry-run only |
| post_text | write | LIVE-VERIFIED | posted_and_verified |
| reply_post | write | LIVE-VERIFIED | target/thread verified |
| quote_post | write | LIVE-VERIFIED | execution-path target evidence |
| post_photo | write | LIVE-VERIFIED | shared media safety pipeline |
| reply_photo | write | DONE | URL-capture gap remains |
| quote_photo | write | LIVE-VERIFIED | dual attachment verification |
| post_multi_image | write | LIVE-VERIFIED | ordered manifest/exact count |
| reply_multi_image | write | LIVE-VERIFIED | shared harness/thread-target |
| quote_multi_image | write | LIVE-VERIFIED | shared harness/dual attachment |
| delete_post | write | LIVE-VERIFIED | id-scoped, kill-before-confirm, tombstone verify |

## Safety kernel / transaction surfaces

Legacy live path:

- token-bound confirmation;
- registry-controlled four-tier risk classification;
- global + per-action token buckets;
- journal-hydrated dedupe/budgets;
- directional compensation;
- deterministic text normalization;
- ordered immutable media manifest + SHA-256 binding;
- composer read-back;
- final kill check;
- identity-aware post-submit verification;
- abort-and-cleanup before submit.

M5 layer-3 additions:

- `EffectPolicy` replay/durability derivation with evidence-backed SAFE claims;
- `EffectLedger` at `.webwire/effects.ndjson`;
- sealed `ApprovalGrant` / `EffectAttempt` lifecycle;
- stable attempt-owned `effect_id`;
- reservation-start latch;
- process-local `CommitGateway` protocol lock;
- generation-based critical kill revocation + authorization epoch;
- direct authorization-epoch fence at final mint/consume;
- callback-free external hot-file refresh at final authority boundaries;
- policy fence across live-policy validation/consume;
- exact-object `EffectPermit` and canonical-attempt lineage;
- private immutable intent snapshot across blocking commit work;
- non-empty persistable target lineage before authority creation;
- final permit-clock sample before atomic grant `spend_if_live()`;
- atomic grant-clock liveness/binding validation + `ACTIVE → SPENT`;
- boundary-time consume expiry check;
- monotonic default clocks for grant/permit authority TTLs;
- durable terminal outcomes before in-memory terminalization;
- strict JSON evidence and reserved correlation-key protection;
- per-path process-local ledger writer serialization;
- exact-fact re-durability after ambiguous fsync.

## Broker surfaces

| Broker | Purpose | Methods / direction |
|--------|---------|---------------------|
| ReadOnlyBroker | read-only DOM surface | navigate, observe, extract, query, enumerate, scroll, tabs |
| WriteBroker | current live mutation surface | semantic directional write methods; still routed via legacy WriteKernel until M5 layer 5 |
| DownloadBroker | local filesystem output | resolve image URL, download image |
| Scoped authorities | M5 layer 4 | not yet implemented; will expose only approved semantic mutation ports |

## Module map (selected)

| File | Role |
|------|------|
| `config.py` | WebWireConfig |
| `session.py` | browser lifecycle + cookie persistence |
| `envelope.py` | ActionResult adapter |
| `broker.py` | ReadOnlyBroker |
| `write_broker.py` | semantic write surface |
| `download_broker.py` | local-output boundary |
| `dispatcher.py` | invocation entry point |
| `registry.py` | capability registry |
| `journal.py` | legacy/audit NDJSON journal |
| `ports.py` | narrow write-port protocols |
| `safety/write_kernel.py` | current live WriteKernel pipeline |
| `safety/effect_policy.py` | M5 replay/durability/authority policy |
| `safety/effect_ledger.py` | M5 durable safety ledger |
| `safety/execution_models.py` | M5 ApprovalGrant / EffectAttempt state machines |
| `safety/commit_gateway.py` | M5 layer-3 CommitGateway / EffectPermit |
| `safety/kill_switch.py` | generation-based kill + critical revocation fence |
| `safety/risk_registry.py` | action risk truth |
| `safety/token_bucket.py` | per-action/global circuit breaker |
| `safety/dedupe.py` | legacy journal-hydrated semantic dedupe |
| `safety/media_manifest.py` | ordered media manifest |
| `safety/media_compose.py` | shared media-compose harness |
| `safety/media_verify.py` | post-submit media verification |
| `safety/post_submit.py` | identity-aware post-submit verifier |
| `capabilities/*.py` | capability implementations |

## Known gaps / open items

- [ ] **M5 L4 scoped authorities** — next build-order layer.
- [ ] **M5 L5 capability migration** — concrete browser mutations do not yet all
  cross CommitGateway.
- [ ] **M5 L6 RecoveryGuard** — ledger can project unresolved facts, but startup
  hydration + mutation denial are not integrated.
- [ ] **M5 reconciliation semantics** — `EFFECT_UNKNOWN` is intentionally
  terminal in layer 3; later reconciliation needs explicit evidence-bearing
  resolved states/events rather than overwrite.
- [ ] **Cross-process EffectLedger writers** — unsupported by current
  single-process contract; would need stronger OS/database coordination.
- [ ] **External kill-file cross-process atomicity** — final-boundary
  re-observation closes long stale windows, but strict ordering with a separate
  file writer requires a cooperating cross-process lock/protocol.
- [ ] **Windows durability runner evidence** — implementation models Windows
  honestly, but CI currently runs Ubuntu only.
- [ ] **Like/unlike BEST_EFFORT evidence** — M5 intentionally keeps these
  `UNKNOWN`/`REQUIRED` until the concrete broker gets state-first/coexistence
  regressions comparable to bookmark/remove-bookmark.
- [x] ~~Bookmark mutation-as-probe~~ — fixed: directional state-read-first
  behavior; already-bookmarked is zero-mutation already-satisfied.
- [x] ~~M4c quote_multi_image~~ — LIVE-VERIFIED 2026-09-23.
- [x] ~~M4a runtime test gap~~ — failure paths covered.
- [ ] reply_photo URL capture gap.
- [ ] Phase 1b edge cases need real fixtures.
- [ ] Screenshot capture plumbed but unimplemented.
- [ ] No CLI yet.
- [ ] Cookie-only persistence remains fragile.
- [ ] Profile display_name extraction unreliable.
- [ ] Super-Browser download() Patchright bug bypassed in DownloadBroker.
- [ ] Future: persistent-context feature in Super-Browser.
- [ ] Future: follow/unfollow capability.
- [ ] Future: Phase 5 analytics.

## Test fixtures

- **Current target post:** `https://x.com/infaag/status/2102451358305771541`
  (2026-09-22 fixture; prior M1 fixture was deleted).
- **Test images:** `.webwire/test-media/test_red.png`,
  `.webwire/test-media/test_blue.png`.
- **Session:** `.webwire/session.json`; auth validity is always re-established by
  `whoami`, not inferred from persistence.

## History

- 2026-09-24 (t): **M5 L3 ATOMIC APPROVAL-SPEND CLOSE-OUT.** Final exact-head
  Codex review of `400a56e` found one new P2: grant expiry could be validated,
  then cross expiry while a separately injected gateway-clock call ran, after
  which low-level `grant.spend()` would still transition `ACTIVE → SPENT` and
  mint a permit. Independently verified. `ApprovalGrant.spend_if_live()` now
  refreshes the grant's own monotonic clock, validates intent/actor/policy/epoch,
  and performs `ACTIVE → SPENT` under one grant lock. CommitGateway now samples
  the permit clock first in the final mint sequence, re-observes kill state, then
  uses `spend_if_live()` under the direct epoch fence; there is no unrelated call
  between final grant validation and spend. Regressions include a model-level
  expiry-at-spend case and an end-to-end REQUIRED case that forces grant expiry
  during the final gateway-clock read and requires `RESERVED → NO_EFFECT`, no
  permit, and an `EXPIRED` grant. Runtime `7b0e60b`: **469 tests**, Ruff clean,
  mypy clean over 51 source files, Python 3.11/3.12 green (CI #113). Grant and
  permit clocks remain independent domains; no simultaneous cross-clock sample
  is claimed. Layer 4 scoped authorities remains next.
- 2026-09-23 (s): **M5 L3 MAINTAINER/CODEX CLOSE-OUT.** After the prior
  `fb5620a` candidate, a fresh maintainer-first pass found and fixed: direct
  authorization-epoch mint/consume races (epoch fence); unsupported
  like/unlike SAFE/BEST_EFFORT classification (downgraded to
  `UNKNOWN`/`REQUIRED` pending broker-level proof); stale permit expiry sampled
  before blocking consume fences (fresh boundary-time monotonic check); and
  external hot-file activation observed only at gateway entry (callback-free
  final-boundary refresh, with cross-process atomicity explicitly not claimed).
  Frozen runtime `3c5e845` passed **465 tests**, Ruff, and mypy on Python
  3.11/3.12 (CI #104), then received an exact-head Codex review whose only new
  finding was the already-known normative policy-table drift. Enumerating older
  unresolved review threads surfaced one still-valid P2: an empty BEST_EFFORT
  target could mint/consume authority but later fail EffectLedger terminal
  persistence. Independently reproduced on the current runtime and fixed at the
  gateway pre-mint boundary with `target_missing`; BEST_EFFORT bookmark
  regressions cover empty target type and id. Final reviewed runtime
  `28156253`: **467 tests**, Ruff clean, mypy clean over 51 source files,
  Python 3.11/3.12 green (CI #107). Layer 4 scoped authorities remains next.
- 2026-09-23 (r): **M5 L3 FINAL CLOCK-DOMAIN HARDENING.** Targeted final Codex
  review found one additional P2 after the 455-test candidate: process-local
  grant/permit authority TTLs defaulted to `time.time()`, so wall-clock rollback
  could extend authority and a forward correction could expire it early.
  Independently verified and fixed: `ApprovalGrant`, `ApprovalGrantStore`, and
  `CommitGateway` now default to `time.monotonic()` for elapsed-time authority;
  injected clocks remain supported. `EffectLedgerRecord.timestamp` remains UTC
  wall-clock provenance. Regression `test_m5_monotonic_authority_clock.py`
  locks grant/store/gateway clock defaults plus ledger timestamp separation.
  Runtime `165527ce`: **458 tests**, Ruff clean, mypy clean over 51 source files,
  Python 3.11/3.12 green (CI #90). Normative design updated with monotonic-clock
  invariant and T19 acceptance case. Layer 4 scoped authorities remains next.
- 2026-09-23 (q): **M5 LAYER 3 REVIEWED CANDIDATE (PR #4).** CommitGateway /
  EffectPermit implemented and subjected to the repository's maintainer-first
  exhaustive review procedure. Frozen maintainer first pass found and fixed:
  ledger history under-constraint; same-path validate→append TOCTOU; ambiguous
  fsync retry; unstable retry effect identity; reservation-failure/clean-release
  conflation; permissive durable evidence; mutable lifecycle fields; and the
  changed concurrency contract implied by pre-I/O reservation latching.
  Runtime hardened to immutable lineage + monotonic ledger history, per-path
  writer lock, exact-fact re-durability, stable attempt-owned effect_id,
  reservation_started latch, sealed grants/attempts, strict JSON evidence,
  canonical permit/attempt objects, durable outcome-before-terminalization,
  permit retention/expiry closure, policy/kill/claim concurrency fencing, and
  generation-based critical revocation. Frozen runtime `e6c300e` then received
  an independent blind Codex review. Codex-only P1: mutable WriteIntent could be
  changed during blocked REQUIRED fsync, validating intent A but minting intent
  B — fixed by one private immutable `_IntentSnapshot`. Codex P2 independently
  converged with a post-freeze maintainer temporal concern: permit TTL started
  before slow reservation/pruning — fixed by gateway-clock mint-time TTL.
  Same temporal pass added post-durability grant/epoch revalidation and durable
  `RESERVED→NO_EFFECT` pre-permit closure when approval becomes invalid; close
  failure remains unresolved/fail-closed. An initial fix incorrectly crossed
  grant/gateway clock domains and CI rejected it (447 pass / 8 fail); corrected
  implementation keeps grant expiry on the grant clock and permit TTL on the
  gateway clock. Verified runtime `db7c948`: **455 tests**, Ruff clean, mypy
  clean over 51 source files, Python 3.11/3.12 green. Normative design aligned
  in `a91028a`. Layer 4 scoped authorities is next; no exactly-once claim.
- 2026-09-23 (p): M5 LAYER 2 (PR #3): ApprovalGrant / EffectAttempt models
  per frozen spec sections 5-7. Grants: ACTIVE → SPENT | EXPIRED | REVOKED
  (terminal, one-directional), orthogonal CAS claim (claimed_by; NOT a state),
  bindings validated on every claim (intent_hash / actor / policy_binding /
  authorization_epoch — the T10/T11 prerequisites), bounded precommit attempts
  (default 3; exhaustion stops automation but never consumes the approval),
  lazy EXPIRED demotion, epoch mismatch REVOKES. Attempts: PREPARING →
  NO_EFFECT | RESERVED → EFFECT_CONFIRMED | EFFECT_UNKNOWN, unfenced effects
  skip RESERVED; outcomes never release or spend — grant transitions stay
  explicit gateway operations. The frozen spend rule ships as TWO calls the
  gateway composes under the claim (mark_reserved; spend after the durable
  fsync) — the ordering contract is documented at spend(). Store: ephemeral,
  in-memory, zero persistence surface by design (test-locked); grants carry
  the store's injected clock for deterministic expiry. T13 and T14 run
  end-to-end at model level. Codex review resolved: P1 policy_binding was
  stored but never compared — now a required claim argument, mismatch denies
  (approval under P1 cannot execute under P2, per the spec's own binding
  rule); P2 grant-taking transitions verify grant identity. Suite 322.
- 2026-09-23 (o): M5 LAYER 1 (PR #2, squash c15c3ce): EffectPolicy + durable
  EffectLedger primitives per frozen build order. Four-axis policy with
  derivation locks (UNKNOWN/non-idempotent/residual-effect → REQUIRED;
  public-amplifying and content-irreversible → REQUIRED regardless of replay
  safety); default table from risk-registry truth; binding_hash for grant/
  permit lineage. Ledger at .webwire/effects.ndjson: fsync-backed durable
  appends that PROPAGATE failure, fail-closed corruption, unresolved RESERVED
  projects as EFFECT_UNKNOWN without rewriting evidence, state-directory
  ancestry persisted on creation (win32 honestly lacks a dir-fsync primitive).
  Codex review: win32 directory behavior regression-locked; coerced identities
  replaced by genuine-string validation; per-action SAFE_TO_RETRY uncertainty
  axis removed because it contradicted the frozen global uncertainty rule.
  Suite 301.
- 2026-09-23 (n): Pre-M5 bookmark made semantic and state-preserving.
  `click_bookmark` reads state first and never touches removeBookmark;
  `click_remove_bookmark` added as mirror. Broker-level four-state/topology
  tests prove replay-safe directional behavior. Suite 279.
- 2026-09-23 (m): M5 design FROZEN and recorded (`docs/M5_DESIGN.md`; baseline
  7d081b9). Unified approval-spend rule, four-axis EffectPolicy,
  ApprovalGrant/EffectAttempt separation, authorization-epoch revocation,
  effect-knowledge states, RecoveryGuard requirement, journal/ledger split,
  T1-T14 acceptance tests, seven-step build order, and negative guarantees.
- 2026-09-23 (l): STABILIZATION BATCH complete. New home pushed; CI foundation
  with offline Super-Browser stub; Ruff 264→0; mypy 117→0; single-photo paths
  migrated to shared harness and gained exact-count gate. Suite 272.
- 2026-09-23 (k): delete_post LIVE-VERIFIED. Registry/bucket/broker/delete
  capability landed; id-scoped article → caret → Delete → confirmation;
  kill rechecked before confirm; tombstone verification. Live diagnostics caught
  coroutine wrapping, pre-hydration existence, and missing asyncio bugs.
- 2026-09-23 (j): M4c LIVE-VERIFIED — quote_multi_image posted and fully
  verified against fixture; media_count 2/2; quote attachment by execution path;
  actor in dedupe key; write facts journaled. v0.2 media tranche complete.
- 2026-09-23 (i): health capability-selector probes closed residual risk.
  DOM-direct polling; shell 5/5, capability 3/3, ready=true. Suite 258.
- 2026-09-22 (h): M4c code complete. Live first attempt cleanly aborted on
  exact-count mismatch; diagnosed quoted-author avatar over-count; attachment
  count changed to blob-first with legacy fallback.
- 2026-09-22 (g): M4b complete. Shared media-compose harness extracted
  behavior-preservingly; reply_multi_image built on it. Live run caught
  first-article scoping and fixed-sleep hydration verifier bugs; both fixed.
- 2026-09-22 (f): health probes fixed. Root cause: probes ran before X client
  render and inferred from observe snapshot. Added direct selector battery and
  hydration polling. Live 5/5, ready=true. 237 tests.
- 2026-09-22 (e): fixture post landed via post_text and live validation caught
  un-awaited whoami hook; actor binding fixed and regression-tested. 230 tests.
- 2026-09-22 (d): final kernel-hygiene batch — actor identity, verify gating,
  honest states, dry-run no-dedupe. 229 tests.
- 2026-09-22 (c): kernel policy stage now verifies against risk registry;
  unknown action / metadata drift fail closed. 221 tests.
- 2026-09-22 (b): media rate-limit bypass closed by normalizing media
  action_types to base post/reply/quote buckets. 216 tests.
- 2026-09-22: architecture review + live E2E. Found dead dedupe hydration,
  media rate-limit bypass, unused risk registry. Journal write-fact hydration,
  rotation, and fail-open legacy memory-store behavior implemented. 212 tests.
- 2026-07-12: M4a runtime blocker closed with 7 failure-path tests. 203 tests.
- 2026-07-12: v0.2 M1-M4a complete. 17 capabilities, 196 tests, 27 commits.
- 2026-07-09: Phase 4 complete (post_text, reply_post, quote_post — all
  live-verified). v0.1 tagged.
- 2026-07-08: Phase 0a-LIVE PASSED through Phase 3b. Session persistence,
  write-safety kernel, bookmark + like live-verified.
