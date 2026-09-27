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
  effect evidence governs replay; evidence-bearing operator reconciliation may
  later resolve durable uncertainty without rewriting the original effect fact.

## Current version

**v0.3 stabilized live path + M5 effect-transaction boundary complete + M6
reconciliation/qualification Layers 1–7 implemented/qualified.**

Canonical M5 runtime baseline after the full M5 build order:

```text
m5_runtime_baseline = 0c62402ae01b50d7662b3978cbf2bee4109aa035
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

M6 builds orthogonal reconciliation history and qualification on top of the M5
transaction boundary:

```text
1. ReconciliationRecord + ReconciliationLedger             MERGED     PR #11  ec077eaeacf9f5928f32276eba102f5848682236
2. Composite RecoveryProjector + publication fence          MERGED     PR #12  cc3aac43f2085c6338dffaec465a129b1da317f1
3. Confirmation epoch + monotonic confirmation authority    MERGED     PR #13  4bbca5e2f8b2388336c8274c5de45fd16595e2da
4. ReconciliationAuthority + coordinator/operator workflow  MERGED     PR #14  0f40959ea74fe8f8ddeb77c5474512c5bccb9c18
5. Fault/restart/corruption/concurrency qualification       MERGED     PR #15  e1e3eeb679cf54b5707c88b0e82e3db4a04d9319
6. Windows durability qualification                         MERGED     PR #16  7b1bf7ee044ba653371e44d4abac7f7254b2c14d
7. Evidence-driven replay-safety qualification              QUALIFIED  PR #17
```

Normative M6 contract: `docs/M6_DESIGN.md`. Layers 5–7 are qualification layers:
production code changes only where target-platform/fault/broker evidence
falsifies an existing invariant. Layer 6 corrected three EffectLedger
portability/corruption defects while keeping the Windows persistence claim
strictly bounded to the environment and primitives actually tested. Layer 7
qualified the supported like/unlike broker and terminal-evidence mechanics but
did **not** qualify a replay-policy promotion: like/unlike deliberately remain
`ReplaySemantics.UNKNOWN` / `DurabilityPolicy.REQUIRED` because repository
broker/DOM evidence cannot establish absence of residual public-engagement
service effects.

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
   SAFE replay classifications require concrete broker-level evidence and any
   stronger service-side replay claim must be separately established.
8. **Writes are semantic and directional.** State-set operations must not
   collapse into unsafe toggles. Bookmark/remove-bookmark are proven directional.
   Layer 7 qualifies the supported like/unlike directional broker/evidence
   mechanics under tested ambiguity/hydration/staleness/ownership cases, but the
   public-engagement replay classification remains conservative
   `UNKNOWN`/`REQUIRED` because no evidence establishes absence of residual
   platform effects.
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
    dedupe, rate-limit, approve, recover, reconcile, or authorize a mutation.
19. **Dedupe TTL and token-bucket budgets are process-local defense in depth.**
    Restart resets those in-memory windows. A future cross-restart budget/dedupe
    requirement needs a dedicated durable policy store, not best-effort audit
    data.
20. **Same-process least authority is not a hostile-code sandbox.** Untrusted
    extensions require stronger process/OS isolation and no raw-browser escape.
21. **Historical uncertainty is immutable.** M6 never changes M5 `RESERVED` or
    `EFFECT_UNKNOWN` history into a different M5 outcome; reconciliation is a
    separate append-only evidence-bearing history.
22. **Reconciliation evidence is not reconciliation authority.** Read/evidence
    collection may inspect and propose; only explicit local operator authority
    can commit one terminal reconciliation verdict.
23. **Reconciliation lineage is exact.** Every terminal reconciliation binds to
    one existing effect id and its canonical first-record semantic/action/intent/
    policy/actor/target lineage plus a canonical evidence hash.
24. **Visible bytes are not automatically durable authority.** Ambiguous
    reconciliation append durability keeps recovery unavailable until exact-fact
    re-durability succeeds; fresh-process startup re-establishes current file
    durability before trusting a surviving reconciliation file.
25. **Reconciliation publication and confirmation revocation are ordered.** The
    process-local publication fence plus confirmation epoch ensures no old token
    can become usable merely because reconciliation cleared recovery.
26. **Reconciliation clears only recovery uncertainty.** Kill, policy, actor/
    target binding, rate limits, dedupe, fresh confirmation, grant/permit rules,
    and all other independent gates remain authoritative.
27. **Old execution authority never resumes.** Reconciliation does not restore or
    reconstruct an old ApprovalGrant, EffectPermit, confirmation token, claim, or
    M5 attempt. Any future mutation is a fresh normal invocation.
28. **Terminal reconciliation is unique in M6.** A second contradictory terminal
    verdict is corruption, not latest-row-wins or silent supersession.
29. **M6 authority and writer coordination remain process-local.** Same-path
    objects share canonical in-process fences/registries; concurrent independent
    processes are outside the current claim.
30. **Qualification claims stay bounded.** Layer-6 evidence establishes the two
    safety-ledger protocol on the tested GitHub-hosted Windows Server 2025 /
    CPython 3.11/3.12 environment but not portable storage semantics or
    cross-process linearizability. Layer-7 evidence establishes the supported
    live broker/terminal-evidence directional mechanics for like/unlike under the
    tested DOM and browser-lease cases, but not absence of notifications,
    callbacks, analytics, counters, or other service-side residual effects and
    therefore not a `SAFE_STATE_SET` / `BEST_EFFORT` promotion.

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
- Scoped authorities expose only the approved semantic mutation surface.
- Capability migration routes bookmark/like, text, reply, quote, media, and
  delete writes through that boundary and disables supported legacy mutation
  fallback.
- RecoveryGuard turns unresolved durable effect facts into enforced pre-browser
  semantic replay denial and refreshes within the same process as well as at
  startup.
- The invocation journal has no safety-state input role. It remains best-effort
  audit evidence with redaction/rotation.

## M6 — evidence-bearing reconciliation boundary

The current recovery authority path is:

```text
EffectLedger (immutable M5 history)
       +
ReconciliationLedger (terminal evidence-bearing reconciliation)
       |
       v
RecoveryProjector
       |
       v
RecoveryGuard
       |
       +--> unresolved -> block matching semantic replay
       +--> terminally reconciled -> clear only this recovery contribution
```

Terminal local reconciliation is ordered as:

```text
publication fence
  -> coordinator protocol lock
  -> canonical CommitGateway lifecycle fence
  -> validate target + operator authority
  -> advance confirmation epoch
  -> commit authority to exact frozen fact
  -> append/re-durable ReconciliationLedger fact
  -> consume reconciliation authority
  -> refresh/publish RecoveryGuard composite projection
```

Layer-5 qualification adds genuine fresh-process crash/restart coverage and
integrated publication/concurrency tests rather than treating object
reconstruction inside one pytest interpreter as equivalent to process death.
Layer-6 qualification adds actual Windows Server 2025 runs for both safety
ledgers. On CPython 3.11.9 and 3.12.10, the focused Windows suite qualifies
nested path creation, writable-handle `os.fsync`, exact-fact re-durability,
startup reconciliation re-durability, ambiguity handling, normalized same-path
identity, corruption/torn-tail fail-closed behavior, and fresh-process composite
recovery. The parent-directory hook remains an explicit Windows no-op; no
portable directory-fsync or broader storage-stack guarantee is claimed.

Layer-7 qualification hardens the supported live like/unlike path around a
qualified direct-target directional-state reader/click seam and a matching
terminal like-evidence reader. Contradictory, missing, hidden, disabled, nested,
duplicate, hydrating, and stale controls are fail-closed under the tested cases;
already-satisfied requests are zero-mutation; composer ownership blocks competing
navigation. This qualifies the local broker/evidence mechanics only. The effect
policy remains `UNKNOWN` / `REQUIRED` because external public-engagement residual
side effects are not established absent.

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
| M6 L1 | ReconciliationRecord + durable ReconciliationLedger | MERGED — PR #11 |
| M6 L2 | composite RecoveryProjector + publication fence | MERGED — PR #12 |
| M6 L3 | confirmation epoch + monotonic token authority | MERGED — PR #13 |
| M6 L4 | reconciliation authority + coordinator/operator workflow | MERGED — PR #14 |
| M6 L5 | crash/fault/restart/corruption/concurrency qualification | MERGED — PR #15 |
| M6 L6 | Windows durability qualification for both safety ledgers | MERGED — PR #16 |
| M6 L7 | evidence-driven replay-safety qualification | QUALIFIED — PR #17 |

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
| `.webwire/effects.ndjson` | immutable M5 effect facts | fsync-backed; authoritative; fail-closed |
| `.webwire/reconciliations.ndjson` | M6 terminal reconciliation facts | fsync-backed; append-only; exact-fact re-durability; fail-closed |
| `RecoveryProjector` | validates/joins effect + reconciliation histories | derived authority projection; contradiction fails closed |
| `RecoveryGuard` | unresolved semantic replay denial | composite derived enforcement; publication-fenced |
| `ReconciliationAuthority` | exact local operator-authorized terminal fact | ephemeral; monotonic TTL; single-use/committed continuation semantics |
| `ConfirmationState` | human-confirmation authority + epoch | ephemeral; monotonic TTL; process-local synchronized revocation |
| ApprovalGrant / EffectPermit | M5 approval/execution authority | ephemeral, fenced, monotonic TTL |
| DedupeStore | repeated semantic-write suppression | process-local only |
| TokenBucket | per-action/global circuit breaker | process-local only |
| `.webwire/journal.ndjson` | invocation audit / diagnostics | best-effort output only; zero recovery/reconciliation authority |
| `.webwire/session.json` | cookie/session convenience | never actor authority |

## Broker / authority surfaces

| Surface | Purpose |
|---|---|
| `ReadOnlyBroker` / leased read broker | coordinated browser reads/evidence |
| `DownloadBroker` | bounded local filesystem output |
| scoped M5 authorities | approved semantic mutation ports |
| `M6ReplayQualifiedWriteBroker` | supported live like/unlike directional state/click hardening while preserving M5 scoped authority |
| `M6ReplayQualifiedEvidenceReader` | supported terminal like-state evidence using the same qualified semantics under the browser lease |
| M5 scoped/live write broker internals | canonical permit-consume mutation seams |
| `ReconciliationOperatorSession` | local proposal/confirmation/reconciliation workflow |
| `ReconciliationCoordinator` | canonical local terminal reconciliation ordering |
| legacy raw WriteBroker path | unavailable from supported Dispatcher migration path |

## Module map — selected

| File | Role |
|---|---|
| `dispatcher.py` | invocation entry + M5 live routing + local M6 operator-session composition |
| `journal.py` | best-effort audit-only NDJSON journal |
| `m6_replay_qualified_write_broker.py` | Layer-7 qualified live like/unlike target-state and exact-click seam |
| `safety/write_kernel.py` | confirmation/policy shell + recovery gate integration |
| `safety/confirmation_state.py` | process-local confirmation epoch + monotonic token authority |
| `safety/effect_policy.py` | replay/durability/effect-scope policy |
| `safety/effect_ledger.py` | durable M5 effect safety ledger |
| `safety/execution_models.py` | ApprovalGrant / EffectAttempt state machines |
| `safety/commit_gateway.py` | EffectPermit lifecycle + canonical live-attempt ownership/fence |
| `safety/scoped_authority.py` | least-authority semantic ports |
| `safety/m6_replay_qualified_evidence.py` | Layer-7 terminal like evidence under qualified broker semantics |
| `safety/reconciliation_ledger.py` | durable M6 reconciliation fact model/ledger |
| `safety/recovery_projector.py` | exact cross-ledger reconciliation projection |
| `safety/recovery_guard.py` | composite restart + same-process unresolved replay enforcement |
| `safety/reconciliation_authority.py` | ephemeral operator reconciliation authority |
| `safety/reconciliation_coordinator.py` | terminal reconciliation protocol ordering |
| `safety/reconciliation_operator.py` | local human proposal/confirmation workflow |
| `safety/m5_live_runtime.py` | coherent live M5 execution stack, including Layer-7 qualified engagement evidence |
| `safety/dedupe.py` | process-local semantic dedupe |
| `safety/token_bucket.py` | process-local write circuit breaker |
| `safety/kill_switch.py` | generation-based kill + critical revocation fence |

## Known gaps / open items

- [ ] **Cross-process effect/reconciliation/browser coordination:** the current
  safety contract is intentionally single-process. Concurrent independent
  recovery/runtime processes are unsupported.
- [ ] **External like/unlike residual-effect proof:** M6 Layer 7 completed the
  broker/evidence qualification but intentionally retained `UNKNOWN`/`REQUIRED`.
  Promotion requires new evidence that repeated public engagement causes no
  additional meaningful service-side effect; DOM convergence is insufficient.
- [ ] **Terminal reconciliation correction/supersession:** M6 intentionally has
  one terminal verdict and no silent edit/latest-row-wins semantics. Corrective
  history would require a future explicit design.
- [ ] **Automatic negative proof:** no generic missing-selector/404/timeout/empty
  read may authorize `CONFIRMED_NO_EFFECT`; accepted action-specific proof
  predicates require separate evidence and authority design.
- [ ] **Same-process authority-root reconstruction:** replacing an entire
  Dispatcher/ConfirmationState authority root on the identical ledger paths
  inside one still-running Python process is not currently qualified. The
  supported topology is one canonical authority root per process/path domain.
- [ ] **External kill-file strict atomicity:** final-boundary re-observation is
  implemented; a non-cooperating external writer cannot share the Python lock.
- [ ] reply_photo URL-capture gap.
- [ ] Phase 1b edge cases need real fixtures.
- [ ] Screenshot capture is plumbed but not implemented.
- [ ] No general CLI yet; reconciliation exposes a narrow local operator API,
  not a generic workflow/CLI framework.
- [ ] Cookie-only persistence remains fragile.
- [ ] Profile display_name extraction remains unreliable.
- [ ] Future follow/unfollow and analytics work.

## Test fixtures

- Current target post: `https://x.com/infaag/status/2102451358305771541`.
- Test images: `.webwire/test-media/test_red.png`,
  `.webwire/test-media/test_blue.png`.
- Session: `.webwire/session.json`; auth validity is re-established by `whoami`.

## History

- **2026-09-27 — M6 Layer 7 replay-safety qualification (PR #17).** Started from
  exact merged Layer-6 baseline `7b1bf7ee044ba653371e44d4abac7f7254b2c14d`.
  The frozen maintainer-first pass found the inherited target-state reader could
  misclassify simultaneous like/unlike controls, required direct stale/hydration
  and browser-lease evidence, and established a claim blocker: repository DOM
  behavior cannot prove absence of notification/callback/analytics/counter or
  other public-engagement residual effects. The supported live path now uses
  `M6ReplayQualifiedWriteBroker` plus `M6ReplayQualifiedEvidenceReader` with
  direct-target control ownership, bounded hydration, exact directional
  cardinality, conservative visibility/disabled checks, exact post-authority
  revalidation, no opposite/page-global fallback, and equivalent terminal
  like-state verification under the browser lease. A distinct adversarial pass
  found and corrected four integration/authority defects: hidden/disconnected
  raw selector authority (R01), nested/duplicate DOM-order authority (R02),
  CSS-hidden/disabled control authority (R03), and terminal like evidence
  bypassing the qualified reader after permit consumption (R04). CI #473 on
  post-adversarial code head `055130d15d54f9945a4f7c639d7138b79f19b795`
  passed **984 tests, 6 Windows-only skipped** on Ubuntu Python 3.11.16 and
  3.12.14, Ruff clean, mypy clean across 86 source files; the Windows Layer-6
  durability matrix remained green with 147 focused tests on Python 3.11.9 and
  the Python 3.12 job green. Qualification result: local broker/evidence
  directional mechanics qualified, external replay-policy promotion **not**
  qualified; like/unlike remain `ReplaySemantics.UNKNOWN` /
  `DurabilityPolicy.REQUIRED`.
- **2026-09-27 — M6 Layer 6 merged (PR #16).** Reviewed head
  `7e34deb35fe33124382576657d0c16a2ee18533c` passed final exact-head CI #461;
  PR #16 squash-merged as `7b1bf7ee044ba653371e44d4abac7f7254b2c14d`
  with an identical reviewed/merged tree. Layer 6 qualified both safety ledgers
  on actual Windows Server 2025 / CPython 3.11/3.12 while retaining explicit
  parent-directory, storage-stack, network-filesystem, whole-browser, and
  cross-process claim ceilings.
- **2026-09-27 — M6 Layer 6 Windows durability qualification candidate (PR #16).**
  Started from exact merged Layer-5 baseline
  `e1e3eeb679cf54b5707c88b0e82e3db4a04d9319`. The frozen maintainer-first
  review found: no actual Windows CI evidence; EffectLedger same-path lock
  identity missing `normcase`; EffectLedger accepting a complete JSON record
  without the append protocol's terminal newline; invalid UTF-8 escaping the
  ledger-corruption exception contract; and a required Windows claim ceiling
  because the parent-directory fsync hook is intentionally a no-op. The
  candidate corrects the three production defects and adds actual
  `windows-latest` CPython 3.11/3.12 qualification. CI #457 on candidate
  `71b365d18d6e6d07462e758e42f176f71a4e9c7b` is green: Windows Server 2025
  (10.0.26100, `windows-2025-vs2026`) ran **147 focused safety-ledger tests** on
  both Python 3.11.9 and 3.12.10; the Ubuntu full gate ran **936 passed, 6
  Windows-only skipped** on both Python 3.11.16 and 3.12.14, with Ruff clean and
  mypy clean across 84 source files. The qualified claim is limited to the
  tested file-handle fsync/re-durability/restart/corruption/path-identity
  protocol; it does not claim portable directory-entry durability, arbitrary
  storage-stack semantics, whole-browser Windows qualification, or
  cross-process linearizability. Close-out documentation is revalidated again
  at exact head before review/merge.
- **2026-09-27 — M6 Layer 5 merged (PR #15).** Final qualification head
  `0b4eca6646fc7c95af45bdc960c2dc3d4c9c450f`; CI #452 green on Python
  3.11/3.12 with **933 tests**, Ruff clean, and mypy clean across 84 source files.
  The maintainer-first/exact-head review added genuine subprocess restart/crash
  qualification, integrated R39 publication races, direct R25/R44 acceptance
  evidence, a real pre-crash pending-token proof, and same-path sibling
  coordinator serialization/confirmation-domain qualification. No independent
  Codex/GitWire action was exposed, so the frozen-design fallback was a distinct
  recorded adversarial second pass with zero additional production findings.
  Squash merge: `e1e3eeb679cf54b5707c88b0e82e3db4a04d9319`.
- **2026-09-27 — M6 Layer 4 merged (PR #14).** Squash merge
  `0f40959ea74fe8f8ddeb77c5474512c5bccb9c18`. Added explicit local operator
  `ReconciliationAuthority`, terminal `ReconciliationCoordinator`, narrow
  operator workflow, canonical same-process live-attempt ownership, exact
  committed-fact continuation, and review-driven hardening before merge.
- **2026-09-26 — M6 Layer 3 merged (PR #13).** Squash merge
  `4bbca5e2f8b2388336c8274c5de45fd16595e2da`. Added synchronized confirmation
  epoch invalidation and monotonic confirmation-token authority semantics.
- **2026-09-26 — M6 Layer 2 merged (PR #12).** Squash merge
  `cc3aac43f2085c6338dffaec465a129b1da317f1`. Added the composite
  RecoveryProjector/RecoveryGuard model and process-local reconciliation
  publication fence while preserving immutable M5 EffectState history.
- **2026-09-26 — M6 Layer 1 merged (PR #11).** Squash merge
  `ec077eaeacf9f5928f32276eba102f5848682236`. Added the strict
  ReconciliationRecord model, canonical evidence hash, and fsync-backed
  append-only ReconciliationLedger with exact-fact durability semantics.
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
