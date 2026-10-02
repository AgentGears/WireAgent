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
reconciliation/qualification Layers 1–7 implemented/qualified + M7 normative
cross-process design frozen; M7 Layer 1 owner-lock mechanism implemented as a
standalone candidate boundary.**

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

M7 centralizes cross-process production authority around the existing M5/M6
root rather than turning every safety primitive into a distributed object:

```text
Design — Cross-Process Authority & Ownership Boundary       MERGED     PR #18  65e3961ed32d090b6a47983901d153838cf99267
1. AuthorityOwnerLock + canonical domain + local registry   CANDIDATE  PR #19
2. AuthoritySession + lifecycle + runtime integration       PENDING
3. instance identity + crash/takeover qualification         PENDING
4. local IPC + bounded pure read/health path                PENDING
5. whoami + non-file writes/confirmation/reconciliation     PENDING
6. local artifact/media ingress-output boundary             PENDING
7. POSIX multi-process qualification                        PENDING
8. Windows Server 2025 multi-process qualification          PENDING
```

Normative M7 contract: `docs/M7_DESIGN.md`. Layer 1 proves the ownership
primitive only. **Mechanism existence is not runtime enforcement:** Dispatcher,
recovery, and browser startup do not yet require `AuthorityOwnerLock`; that outer
ordering becomes mandatory in Layer 2. No cross-process production-runtime claim
is made from Layer 1 alone.

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
    processes are outside the M6 claim.
30. **Qualification claims stay bounded.** Layer-6 evidence establishes the two
    safety-ledger protocol on the tested GitHub-hosted Windows Server 2025 /
    CPython 3.11/3.12 environment but not portable storage semantics or
    cross-process linearizability. Layer-7 evidence establishes the supported
    live broker/terminal-evidence directional mechanics for like/unlike under the
    tested DOM and browser-lease cases, but not absence of notifications,
    callbacks, analytics, counters, or other service-side residual effects and
    therefore not a `SAFE_STATE_SET` / `BEST_EFFORT` promotion.
31. **M7 ownership has one non-expiring outer primitive, but Layer 1 is not yet
    enforcement.** One canonical absolute `state_dir` maps to a process-local
    normalized-domain reservation plus a dedicated OS-held `authority.lock`.
    Lock-file existence/PID/mtime/age never creates authority; controlled release
    is private-handle close. Until Layer 2 wraps Dispatcher/recovery/browser
    startup with this boundary, independent runtime processes remain unsupported.

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

## M7 — cross-process authority boundary

The frozen design adds one outer ownership law around the existing authority
root rather than adding independent cross-process locks to each M5/M6 primitive:

```text
AuthorityOwnerLock                    # M7 outer lifetime boundary
  -> later AuthoritySession/lifecycle
     -> Dispatcher / Recovery / M5 / M6
```

Layer-1 candidate facts:

- configured `state_dir` is frozen to an absolute `resolve(strict=False)` domain;
- in-process identity uses the established `os.path.normcase` posture;
- process-local reservation occurs before OS lock acquisition;
- POSIX uses non-blocking `flock`; Windows uses non-blocking `msvcrt.locking`;
- the dedicated descriptor is non-inheritable for spawn/exec paths;
- a POSIX after-fork child hook closes only the child's inherited descriptor copy
  and never explicitly unlocks the shared open-file description;
- `authority.lock` is retained and carries no PID/mtime/heartbeat/TTL authority;
- controlled release is deliberate private-handle close, with registry identity
  validated before crossing that OS release boundary;
- close/acquisition uncertainty fails closed rather than granting replacement;
- fresh-process tests establish exclusion and clean-successor behavior for the
  primitive itself.

This does **not** yet mean Dispatcher or standalone recovery is cross-process
protected. Layer 2 must acquire the outer ownership boundary before recovery or
browser startup and make it mandatory for supported production entrypoints.

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
| M7 design | cross-process authority/ownership normative contract | MERGED — PR #18 |
| M7 L1 | AuthorityOwnerLock + canonical domain + process-local registry | CANDIDATE — PR #19 |
| M7 L2 | AuthoritySession + lifecycle + runtime/recovery integration | PENDING |
| M7 L3 | instance identity + startup/shutdown/crash-takeover qualification | PENDING |
| M7 L4 | local IPC pure read/health path | PENDING |
| M7 L5 | whoami + non-file-backed writes/confirmation/reconciliation | PENDING |
| M7 L6 | local artifact/media ingress-output boundary | PENDING |
| M7 L7 | POSIX multi-process qualification | PENDING |
| M7 L8 | Windows Server 2025 multi-process lock/IPC qualification | PENDING |

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
| `.webwire/authority.lock` | M7 stable rendezvous for one canonical domain | file existence/content non-authoritative; OS-held owner lock is ephemeral; Layer-1 mechanism only until runtime integration |
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
| `AuthorityOwnerLock` | M7 Layer-1 cross-process ownership primitive; not yet mandatory around runtime entrypoints |
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
| `authority.py` | M7 canonical domain + non-expiring OS owner-lock primitive and process-local owner registry |
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

- [ ] **M7 runtime ownership integration:** Layer 1 provides the canonical-domain
  owner-lock mechanism, but Dispatcher/recovery/browser startup do not yet require
  it. Cross-process production authority remains unsupported until Layer 2 wraps
  the existing authority root and freezes lifecycle ordering.
- [ ] **M7 full process/platform qualification:** forced-death takeover, child
  process modes, POSIX fault/response-loss cases, network/shared-storage claim
  gates, local IPC permissions, and Windows Server 2025 cross-process ownership/
  IPC qualification remain Layers 3–8 as frozen in `docs/M7_DESIGN.md`.
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

- **2026-10-01 — M8 layer 2 review fixes (PR #22): F-09..F-15 resolved.**
  The maintainer-first pass at exact head 81ced8c found three blockers, two
  contract gaps, and an evidence gap; every finding verified against the code
  before fixing. F-09 (blocker): the live Dispatcher never installed a
  RuleStore — the M8 gate was unreachable from ordinary construction. Fixed:
  WebWireConfig.rules_path() added; the Dispatcher constructs ONE RuleStore at
  the authority root and passes it to WriteKernel (long-lived instance safe —
  Layer 1 reloads on every match); dispatcher-level regression proves a
  persisted NEVER denies through normal construction. F-10 (blocker): approver
  was mutable after mint (not in _GRANT_PUBLIC_FIELDS) — sealed; reassignment
  raises GrantStateError (regression-locked). F-11 (blocker): approver was in
  the ledger but NOT in _LINEAGE_FIELDS — reserved/terminal records could
  disagree on provenance without contradiction, and a changed-approver retry
  counted as the same fact. approver added to canonical lineage + exact-fact
  identity; None remains its own legacy lineage (never upgraded to "human");
  both regressions locked. F-12: canonical validate_approver (human |
  rule:<id>, None only for pre-M8 reads) wired at the validator, grant-store
  mint, runtime.issue, ledger validate, and ledger parse. F-13: rule_gate
  metadata now emitted ONLY when a rule matched — no-match responses are
  byte-for-byte pre-M8. F-14: the four weak tests replaced with real
  lifecycle qualification through a real runtime + gateway + file-backed
  ledger (T20 real policy drift, T21 real epoch advancement, T22
  durable-record approver identity, T24 human end-to-end); no
  inspect.getsource remains. F-15: the execute_with_approver seam exposed on
  ALL six migrated adapters (post-text, reply, quote, media, delete adapters
  now delegate with a per-call approver). Suite 1062 (count from the run).
- **2026-10-01 — PR #25 fourth review pass: finishing F-53/F-54 + F-55.**
  F-53 completed: OfflineRecoveryAuthority.acquire() now hydrates BEFORE
  constructing any M6 authority state (lock → EffectLedger → RecoveryGuard
  → hydrate → CommitGateway → fresh ConfirmationState → coordinator →
  session → READY) — the coordinator's process-wide protocol registration
  (strong, bound to its ConfirmationState) is no longer left behind when
  corrupt history fails acquisition, so a same-process retry after repair
  works. The regression proves the reviewer's exact sequence: corrupt
  ledger → acquisition fails → repair → a NEW OfflineRecoveryAuthority in
  the SAME process acquires, exposes the operator surface, and closes.
  F-54 completed on both remaining holes: (a) the injected-manager check is
  now an IDENTITY check — the manager's RETAINED state root must already
  BE the frozen canonical absolute Path (Path equality, not
  resolve-at-validation equality), so a still-relative or un-frozen
  symlinked manager root is refused even when it resolves equal at
  construction time; regression covers the injected-relative-root case
  under chdir. (b) the frozen relative-root warning now carries BOTH the
  relative input and the resolved absolute authority domain, and the
  warning site moved into canonical_config() itself — the single
  canonicalization point — so the REAL build_production_runtime path
  emits it (canonicalizing before constructing both objects); the
  regression drives the actual factory and asserts the warning contains
  both values, replacing the previous workaround that constructed a
  separate Dispatcher just to produce a warning. F-55: the logging-handler
  leak fixed — one handler instance, added and removed. Housekeeping: a
  blanket `ruff format src/ tests/` during this round reformatted 130+
  unrelated files; the collateral was reverted before commit (the diff is
  four implementation/test files plus this STATE entry — five files
  total). Suite 1192 (count from the run).
- **2026-10-01 — PR #25 third review pass: F-49/F-50/F-52/F-53/F-54 —
  non-bypassable boundaries and genuinely fail-closed teardown.** F-49
  (blocker): teardown failure is now fail-closed END TO END — a browser
  whose stop() raises is RETAINED (references kept for retry, never
  reported already-stopped over), SessionManager.stop() returns a hard
  failure instead of swallowing, and Dispatcher.stop() refuses to
  terminalize or release over an unproven root (ownership retained; a
  healed retry completes the shutdown law; a DRAINING session is a valid
  retry, not an error). The interrupted-start path keeps the partial
  browser reference when its stop fails so the authority layer sees the
  ambiguity. Regressions: failing SB.stop() on BOTH paths proves
  ownership retained; the healed-retry then completes. F-50 (blocker):
  OwnedReconciliationOperatorSession exposes NO raw authority escape —
  delegate and coordinator pass-throughs REMOVED; the full supported
  surface (operator_id, list_targets, show_target, prepare_resolution,
  confirm_resolution, resolve) is admitted delegation. M6 composition
  assertions updated to escape-free (hasattr checks + admitted reads).
  F-52: a cleanly quiesced failed start terminalizes the session
  (abort_from_starting: STARTING → TERMINAL after revoke) BEFORE the
  owner handle closes; regression asserts TERMINAL at the release
  boundary and already-stopped thereafter. F-53: the offline recovery
  owner follows frozen §14.2 — acquire → guard.hydrate() (projects BOTH
  safety ledgers) → construct root → activate; corrupt effect OR corrupt
  reconciliation history fails ACQUISITION with RecoveryGuardUnavailable
  before any operator authority exists, and the lock is released for a
  successor (both regressions added; the reconciliation ledger file is
  reconciliations.ndjson). F-54: build_production_runtime canonicalizes
  the config BEFORE constructing the session manager AND the dispatcher;
  Dispatcher.__init__ warns on a relative state_dir (the frozen
  production diagnostic) and REFUSES an injected session manager whose
  state root does not match the canonical authority domain (reads
  _ww_config); regressions pin one absolute domain across a CWD change
  on the real factory path and refuse a mismatched manager. Test-double
  updates: the serialization _SB double now implements stop() (the
  fail-closed contract requires a stoppable browser object); the gate SM
  mirrors the real stop contract. Suite 1189 (count from the run).
- **2026-10-01 — PR #25 second review pass: F-43..F-48 — the frozen
  session/lifecycle architecture, implemented.** The review's diagnosis,
  accepted: all six blockers pointed at one missing abstraction — Layer 2
  as built attached the raw lock to the Dispatcher lifecycle instead of
  the frozen AuthoritySession + AuthorityServiceLifecycle topology. The
  repair implements it: canonical absolute state_dir is frozen BEFORE any
  M5/M6 construction (F-46; a CWD change can no longer split the lock
  domain from the safety-state domain); the AuthoritySession
  (webwire/authority_session.py, stdlib-only) is the owner-wide fence —
  STARTING → READY → DRAINING → TERMINAL, admission state-gated, owner-
  wide drain on one active count, revokers run before terminalization,
  TERMINAL is permanent; start() acquires ownership, constructs the
  session, registers the confirmation-epoch revoker, and starts the root
  under an ASYNC lifecycle gate so start/stop can never interleave (F-45;
  the gate must be asyncio, not threading — a threading lock taken by a
  waiting coroutine blocks the loop itself); create_reconciliation_
  operator_session is gated on a READY session and every operator
  operation is admitted owner work (F-43); a failed/cancelled start
  releases only after the session manager proves the partial root
  quiescent — SessionManager hardened to stop a partial _sb and to clean
  up a cancelled browser start (F-44); stop() runs the shutdown law in
  order — begin_drain → invoke-lock + session drain (the blocking drain
  runs in an executor thread so the loop stays live for the in-flight
  work) → revoke (epoch advance; F-47) → retire browser → terminalize →
  release the owner lock LAST; and a TERMINAL session never restarts
  (F-47). The offline recovery owner (webwire/offline_recovery.py)
  implements the standalone entrypoint: acquire-or-refuse the domain,
  build the authority root under the lock, admitted operator access,
  same shutdown law on close. Session-layer invocations are admitted
  through the session too (F-48). Pre-Layer-2 test fixtures that drove
  unstarted Dispatchers were updated to the started lifecycle
  (serialization + M6 composition); three of my own build bugs were
  caught before push (M5-stack stub shape, asyncio-vs-threading gate,
  blocking drain freezing the loop). 8 new regressions, one per finding
  plus the offline-owner subprocess qualification. Suite 1182 (count from
  the run).
- **2026-10-01 — M7 LAYER 2 BUILT (PR pending): ownership is mandatory
  around the runtime authority root.** Dispatcher.start() now acquires the
  AuthorityOwnerLock FIRST — before recovery hydration and before any
  browser/session activity — so a losing process fails with a controlled
  authority_busy error BEFORE production browser or safety-write authority
  exists. A start that fails after acquisition (session refused, stack
  failure, exception) releases ownership instead of stranding the domain;
  double-start is refused without releasing. stop() drains the invocation
  lock first (admitted mutation work completes; nothing is overtaken) and
  releases ownership LAST — a successor can never start while admitted
  work might be in flight. The m8 card CLI (a transient authority
  process) surfaces the busy failure as a controlled one-line stderr
  message with exit 1, no traceback. Scope boundaries stated: this wires
  the supported Dispatcher authority root and the CLI busy path;
  standalone recovery entrypoints, lifecycle/stale-session/crash-takeover
  QUALIFICATION beyond the OS-release semantics proven here, and the IPC
  topology are Layers 3+. In-process topology note: the pre-existing
  ReconciliationCoordinator same-path registry already refuses two
  Dispatchers on one state dir inside one process at construction —
  Layer 2's tests therefore model the rival owner with a raw lock (the
  real cross-process shape) and at most one Dispatcher per path per
  process; the cross-process test uses two real subprocesses. 6 new
  tests: loser-fails-before-browser + retry-after-release,
  failed-start-releases, double-start-refused, drain-before-release,
  cross-process refusal + OS-level takeover after kill, CLI busy path
  through the real production factory. Suite 1174 (count from the run).
- **2026-10-01 — PR #19 MERGED (edba2af): M7 LAYER 1 on main — the
  authority-owner lock is real.** Cleared through three fresh-review rounds
  after M8 completed (F-38 Windows-mypy portability via runtime module
  resolution on both lock branches; F-39 merge with M8-complete main
  keeping both CI additions; F-40/F-41/F-42 documentation provenance and
  evidence currency). Layer 1 delivers canonical authority-domain
  identity, process-local owner reservation, the non-expiring OS-held
  owner lock on authority.lock, fork-gated descriptor lifecycle with
  child detach, and fail-stop at every ambiguous seam — standalone, stdlib
  only, nothing yet wired to the Dispatcher. Merged-tree qualification:
  Linux 3.11 1171 passed + 6 platform-skipped = 1177; Ruff clean; mypy
  clean across 92 files on BOTH platform resolutions; Windows focused
  matrix 170 passed + 4 skipped plus rule-store 57; maintainer's local
  Windows gate 1168 + 9. NEXT BUILD (M7 Layer 2): make AuthorityOwnerLock
  mandatory around the supported runtime authority root — Dispatcher
  startup/drain ordering, and a controlled authority_busy path for
  transient entrypoints such as `m8 card`.
- **2026-10-01 — M8 LAYER 4 LIVE-SESSION QUALIFICATION (record only; no
  architectural changes).** Qualification executed against a real logged-in
  X session per the directed sequence. Environment: Windows 11 (10.0.26200),
  Python 3.12.1, live Super-Browser SDK (C:/Next-Era/Super-Browser,
  LAUNCH mode), account **infaag**, isolated state dir with a copy of the
  persisted session; harnesses: scripts/qualify_m8_layer4_live.py (full)
  and scripts/qualify_m8_sc67_live.py (focused 6/7b re-verification).
  Action types: like (executed), post_text (card-only, denied — nothing
  published), bookmark (not executed). Real effects, disclosed: FOUR
  public likes on ordinary home-timeline posts (one human-approved via
  card; one standing-ALLOW auto-executed; two raw-route token-pair first
  uses). Likes are not reversible through the current capability surface.
  Scenarios, all PASS across the two runs: (0) whoami resolved
  handle=infaag with resolved_handle set; (1) ASK → card rendered
  ('Will like post …', token scrubbed from the returned result) →
  operator approve → verdict allow, execute_ok, ledger
  RESERVED+EFFECT_CONFIRMED approver=human; (2) ASK → deny → zero ledger
  change, phase 2 never invoked; (3) standing ALLOW (like) → NO card,
  one invocation, trace approver=rule:qual-allow-like, ledger pair
  approver=rule:qual-allow-like; (4) NEVER → blocked_by=user_rule BEFORE
  any confirmation carrier, no card, ledger unchanged; (5) above-ceiling
  ALLOW (post) → confirmation_required with ceiling_downgraded=True and
  the honest still-asks card note → denied, nothing posted; (6) rule
  changed between phase 1 and phase 2 → the token-bearing replay was
  denied by the re-evaluated NEVER (blocked_by=user_rule), zero like
  lifecycles in the ledger; (7) malformed token denied (consumed_token),
  reused token denied fail-closed (run A: token_bucket — the rate gate
  precedes token validation; run B: dedupe), caller-supplied token at
  the card boundary raised ValueError; (8) journal carries
  '"confirmation_token":"<redacted>"' with ZERO raw token bytes, and
  every ledger row's approver is exactly human or rule:qual-allow-like.
  Browser-specific findings recorded: (a) whoami identity resolution is
  INTERMITTENT across loads (~1 in 3 observed failures; hydration race —
  markers AppTabBar_Profile_Link/SideNav_AccountSwitcher_Button present
  when resolved; bounded retry in the harness) — a pre-existing known
  fragility, not an M8 defect; (b) timeline permalink availability
  varies per load (3-6 on first viewport; scroll-harvest required);
  (c) the dedupe gate fires BEFORE preview and the rule gate on
  same-target repeats within TTL — each phase-1+ scenario therefore
  needed a distinct real post; (d) BEST_EFFORT actions (bookmark) write
  no ledger rows by design, so ledger attribution evidence requires a
  fenced action (like). The first full run recorded FAILs on scenarios
  6/7b from harness assertion bugs (ledger rows counted instead of
  lifecycles; an over-specified denial reason) — the product outcomes
  were correct in that run and the focused rerun passed both cleanly.
- **2026-10-01 — PR #24 MERGED (8d89827): M8 COMPLETE — all four layers
  of the frozen build order are on main.** The final pass found no new
  findings across the whole interaction surface (token custody,
  caller-supplied authority rejection, chronological approval, payload
  snapshot binding, malformed carriers, journal redaction, runtime
  identity establishment, browser-free rule management, CAS
  re-confirmation, TTL handling, and the ceiling renderer). CI run #575:
  Linux 3.11 1139 passed + 6 platform-skipped = 1145; both Windows
  durability jobs green including the rule-store qualification. M8 as
  shipped: layer 1 the standing-decision rule store (PR #20, 80f427c +
  hardening PR #21, 3ee9df1); layer 2 the kernel three-way gate +
  approver lineage through the single mint seam (PR #22, 6653fdf);
  layer 3 the rule compiler — pluggable API-key model, strict tagged
  model-output schema, canonical owner confirmation, fenced CAS
  persistence (PR #23, 04176da); layer 4 the card surface + Card CLI +
  rule lifecycle (PR #24, 8d89827). Cumulative review: 57 findings
  (F-01..F-57) across eight review rounds, all resolved before merge.
  STANDING BOUNDARIES, stated not implied: the production m8 card path
  is wired per the real lifecycle (start → whoami → resolved actor →
  card → decision → stop) but has not been qualified against a real
  logged-in browser session; per-token cancellation on deny and rule
  removal are outside the frozen layer-4 scope; the single-process
  doctrine (one canonical authority root per state directory, STATE
  366) is contract and construction, not yet boot-time enforcement.
- **2026-10-01 — PR #24 fifth review pass: F-57 (derived-tier composition).
  The one remaining renderer edge.** With unknown actions present and NO
  explicit risk_tiers, the unknown-action branch returned before deriving
  the KNOWN actions' registry tiers — so a persisted rule like
  {post, future_action} reported only the unknown action's latent behavior
  and never told the owner that post (above the standing ceiling) will
  still ASK. The renderer now partitions the vocabulary on that path:
  known actions' tiers are derived from the registry, any above-ceiling
  known tier adds 'the known above-ceiling actions in this rule will still
  ASK', and the unknown-action latent warning stands beside it — composed,
  neither masking the other. Unknown-only selectors keep the pure latent
  warning. Three regressions: {post, future_action} (ASK + latent),
  {like, post, future_action} (three-way: like unaccused, post ASK,
  future latent, no blanket tier claim), and unknown-only (latent, no ASK
  claim). SUPERSEDES the F-54..F-56 entry's implication that 'no explicit
  tiers → latent-authority wording' is universally sufficient: it is
  sufficient only when no KNOWN actions are present. Suite 1145 (count
  from the run).
- **2026-10-01 — PR #24 fourth review pass: F-54..F-56 (final semantic/
edge hardening).** F-55 (high): the F-49 unknown-action branch MASKED the
deterministic tier-ceiling math for mixed selectors — with explicit tiers
all above the ceiling, the old text promised conditional auto-approval
even though every matching registration would still ASK (impossible
condition; frozen §5 requires above-ceiling ALLOW to be honestly ASK).
The renderer now COMPOSES the two facts: unknown action + explicit tiers
all above → 'will still ASK (every named tier is above the allow
ceiling)'; tiers containing below-ceiling → 'may auto-approve only if
later registered at one of the below-ceiling tiers matching this
selector', plus 'matching above-ceiling tiers still ASK' when mixed; no
explicit tiers → the F-49 latent wording stands. Regression locks all
four compositions. F-54: the CLI rejected a caller-supplied
confirmation_token only at CardFlow's boundary — AFTER building the live
runtime (browser start, whoami) and via an uncontrolled traceback. The
reserved-field check now runs immediately after JSON validation: exit 2,
stderr message, and the regression proves runtime_factory was NEVER
called. F-56: a non-string confirmation carrier (dict, number, empty
string) was coerced into an unusable ApprovalCard; the card now requires
a genuine non-empty string carrier — anything else returns the sanitized
result with no card, flowing to the CLI's confirmation-required protocol
error (exit 1). SUPERSEDES the F-48..F-53 entry's 'closed by F-49' claim:
F-49 closed the structural gap only; the semantic composition is closed
here. Suite 1142 (count from the run).
- **2026-10-01 — PR #24 third review pass: F-48..F-53 (the card entry
  point and the audit boundary).** F-48 (blocker): CardFlow.begin()
  forwarded the caller's payload unchanged, so a caller holding a valid
  token could pass it INSIDE begin() — the kernel interprets that as phase
  2 and the mutation executes before any card is rendered or any decision
  asked. The card entry point now REJECTS a caller-supplied
  confirmation_token before invoking anything (reject, not strip — silent
  stripping changes caller intent); regression proves ZERO invocations.
  F-50: policy gates run BEFORE token consumption, so a NEVER installed
  after phase 1 denies while the token is still LIVE — and the Dispatcher
  journaled the token verbatim (the journal is audit data, not an authority
  carrier). _redact_input now redacts confirmation_token BY KEY
  irrespective of value; the regression uses a REAL kernel token, denies
  phase 2 pre-consumption via a late NEVER, proves the SAME token still
  executes after the ban is removed (pre-consumption denial), and asserts
  the exact token bytes never appear in the journal while the redacted
  marker does. F-49: the unknown-action vocabulary check is now
  ORTHOGONAL to risk_tiers — a mixed action+tier selector naming an
  unknown action gets the latent warning too, with the precise conditional
  ("if later registered at a tier matching this selector and below the
  standing-rule ceiling, this ALLOW may auto-approve"). F-51: the
  production runtime now verifies session.resolved_handle after whoami —
  the state the write path actually trusts — not merely the response data
  (a whitespace-only response handle is truthy data but set_resolved_handle
  refuses it; regression covers the blank case). F-52: a
  confirmation-required response with no usable card is now an explicit
  protocol error in the CLI (non-zero exit, stderr message); it previously
  exited 0 via the generic no-card path. F-53: update_strict() REMOVED —
  an unconditional same-ID replace is exactly the stale-write shape F-34
  invalidated, left behind as an attractive footgun; replace_if_current is
  the only lifecycle mutation. Evidence corrections to the entries below:
  the F-41..F-47 entry's claim that "the token exists only inside the
  card" overstated custody (F-48: callers could previously SUPPLY their
  own; F-50: live tokens could reach the journal — both now closed), and
  "F-45 closed" was premature for mixed selectors (closed by F-49).
  Suite 1139 (count from the run).
- **2026-10-01 — PR #24 second review pass: F-41..F-47 (the human/CLI
  boundary).** The review's framing, accepted: the store CAS was the
  strongest part; the remaining blockers were concentrated exactly where
  layer 4 must establish owner-confirmation semantics. F-42 (blocker):
  --yes was preauthorization of an unseen preview, and re-confirm displayed
  AFTER the confirmation flag was consumed — the human decision could
  predate the snapshot on screen. The CLI now renders FIRST and obtains
  the decision NOW through an injectable decision_reader (terminal input
  in production); --yes/--deny flags are removed entirely. Regressions
  prove the decision happens after display (the reader sees the rendered
  words + structure), a "no" mutates nothing, and the store CAS still
  backs the displayed snapshot. F-41 (blocker): the production wiring
  never called start() and never established a whoami-resolved actor —
  the installed path could not reach a card at all. New
  build_production_runtime (both factories injectable, so the WIRING is
  tested): dispatcher.start() → whoami invoke → require ok AND a handle
  (migrated writes refuse without a resolved actor) → return; any failure
  stops the dispatcher before the error leaves; the card command stops it
  in a finally (regression: stop runs even when phase 2 raises). Rules
  commands never build a runtime (regression: a factory that fails the
  test if invoked). F-43: the request snapshot is deep-copied BEFORE the
  first await (regression: the invoke itself mutates the caller's nested
  payload mid-await; the card still replays the phase-1 values). F-44:
  the rules-only CLI is now genuinely browser-independent — the rule
  lifecycle moved to webwire/safety/m8_rule_lifecycle.py (browser-free),
  the CLI lazy-imports CardFlow only inside the card command, and BOTH
  package __init__ files became PEP-562 lazy (webwire: Dispatcher/Session/
  envelope names; safety: kill_switch, scoped_authority, write_kernel,
  reconciliation_coordinator chain, recovery_guard — the coordinator→
  commit_gateway→kill_switch chain was the hidden browser edge), so
  importing a rules submodule no longer transitively imports the browser
  SDK. Qualified by a clean subprocess with the conftest stub path
  REMOVED: the CLI imports, asserts super_browser/envelope/dispatcher/
  write_kernel absent from sys.modules, and runs rules list — passing
  locally even with the real SDK installed, stronger on CI's base
  install. F-45: the unknown-action note no longer promises ASK — it now
  states the three facts: ceiling not verifiable (outside the active
  registry), currently non-executable, and may AUTO-APPROVE if the action
  is later registered below the standing-rule ceiling. F-46: CLI TTL
  errors (0/-1/nan/inf/over-max) are caught as controlled usage failures
  (exit 2) — and reconfirm_rule now rejects non-finite TTLs directly
  (isfinite; NaN previously slipped past the </> comparisons into
  UserRule's constructor). F-47: the sanitizer runs UNCONDITIONALLY before
  any envelope branching, and the policy echo's confirmation_token is
  removed regardless of its runtime type; a drifted envelope whose token
  exists only in the policy echo yields NO card (fail-closed — a
  payload-less token cannot be approved through the surface) and a
  scrubbed result. Suite 1135 (count from the run).
- **2026-10-01 — PR #24 review pass: F-34..F-40 (snapshot binding, token
  custody, the frozen CLI).** The review's principle, accepted: human-visible
  state, confirmation authority, and the mutation performed afterward must
  all bind to ONE immutable snapshot. F-34 (blocker): TTL re-confirm was
  id-keyed only — a same-id ALLOW→NEVER edit between the owner's read and
  the fenced write would be silently overwritten by the stale re-confirm
  (the fence serialized the write but not the read→confirm→write
  transaction). Fixed with compare-and-swap: RuleStore.replace_if_current
  (expected, replacement) under the existing mutation fence — the stored
  rule must EQUAL the reviewed snapshot on every field (decision, selector,
  provenance, source text, timestamps); any change, deletion, or corruption
  conflicts with zero mutation; identity can never be rewritten.
  reconfirm_rule now takes the reviewed UserRule itself, and listing hands
  out the exact immutable snapshot (RuleCard.rule). Regressions lock the
  review's exact race (stale ALLOW re-confirm can never overwrite a newer
  NEVER) and the deleted-rule conflict. F-40 (blocker): begin() returned
  the RAW kernel phase-1 result beside the card — token custody was a
  rendering property, not an API property. The returned result is now
  SANITIZED (deep-copied, token removed from BOTH the payload and the
  policy echo); the token exists only inside the card. deny() still invokes
  nothing; the confirmation subsystem exposes no per-token cancellation, so
  the denial's bound is the token's existing TTL — stated, not implied.
  F-38: the card is bound to the invoke route that created it;
  approve() is parameterless (no caller-selected phase-2 transport). F-39:
  the payload is deep-copied at begin and again at replay — nested caller
  mutation after phase 1 cannot change what is confirmed (regression
  locked). F-35: lifecycle rendering is TOTAL over legal rules — an ALLOW
  naming an action outside the active registry renders 'ceiling not
  verifiable … outside the active registry' instead of raising (a tier is
  never inferred for an unknown action; management display is more total
  than enforcement configuration). F-36: RuleCard carries source_text (the
  owner's original words) and renders them beside the canonical
  description; re-confirm displays words + structure + new TTL. F-37
  (blocker/spec): the frozen Card CLI is now IMPLEMENTED (m8_card_cli.py;
  console entry `m8`): `m8 card CAPABILITY PAYLOAD [--yes|--deny]` (render,
  and decide in-run; without a flag the card is shown and the token dies
  with the process), `m8 rules list`, and `m8 rules reconfirm RULE_ID
  --ttl S [--yes]` (display then CAS-replace; dry-run without --yes).
  Dependencies are injectable — the CLI is fully tested without a live
  session. This SUPERSEDES the previous entry's 'library-only' scope claim,
  which silently redefined layer 4: that was wrong, the frozen build order
  names a CLI and now has one. Rule removal remains deliberately absent
  (the build order names exactly list + TTL re-confirm). 12 net-new tests;
  suite 1128 (count from the run).
- **2026-10-01 — PR #23 MERGED (04176da) + M8 LAYER 4: the card surface +
  rule lifecycle (PR pending).** Layer 3 cleared the maintainer pass at
  6fc5467 (no new blockers; the reviewer's own log check confirmed the
  Windows rule-store step: 147 durability + 57 rule-store tests) and
  squash-merged. Layer 4 implemented per frozen section 8 (m8_cards.py,
  top-level — presentation, not enforcement): CardFlow.begin() wraps any
  invoke callable (the Dispatcher in production) and returns a phase-1
  result plus an ApprovalCard when human confirmation is pending; rule-ALLOW
  and NEVER outcomes return with NO card (nothing to approve). The card
  renders summary/target/current-state/warnings/matched-rule (+ ceiling
  note) via to_dict()/render_text(); the confirmation token is held
  INTERNALLY — never in to_dict, render_text, or repr. approve() replays the
  exact original payload with the held token (phase 2); deny() invokes
  nothing and returns a defined declined outcome; a card is single-use (the
  spent message names which decision consumed it). No new authority: every
  verdict still comes from the kernel through the invoke callable. Rule
  lifecycle per the build order's named two operations: list_rules()
  (enforcement-reader fail-safe — corrupt store lists as empty; canonical
  descriptions via the SAME renderer the compiler's owner-confirmation
  uses; live remaining-TTL/expired state) and reconfirm_rule() (same
  rule_id — attribution identity preserved; same selector/decision/
  provenance; fresh TTL starting at the re-confirm call; TTL bounded by
  the 7-day contract; unknown id fails closed; the write goes through a NEW
  store primitive update_strict — fenced in-place replacement, corrupt
  store refuses with bytes unchanged, every other rule keeps its position;
  re-confirming an expired rule re-establishes it by owner choice).
  Integration test drives the REAL dispatcher phase-1 → card → phase-2
  through the M5 executor + gateway (fake bookmark broker) to a executed
  bookmark. Deliberate scope limits, stated: no rule removal (the build
  order names exactly list + TTL re-confirm; revocation remains editing the
  store, observed by the next match); the surface is a library (a CLI
  program wrapping it against a live session is out of CI-qualifiable
  scope here). 14 new tests; suite 1116.
- **2026-10-01 — PR #23 third review pass: F-30..F-33 (present-null
  widening, deterministic fence falsification, Windows lock qualification,
  closed fences).** F-30 (blocker): the rule-field reducers used
  payload.get() — an explicit JSON null was indistinguishable from an
  omitted field, so {"actors": null} widened an ALLOW to every actor and
  {"ttl_seconds": null} silently took the default lifetime. All rule fields
  now use presence checks: omitted = unspecified; present = must satisfy
  the declared type (null is not a list, not a tier list, not a number).
  Regression matrix covers null for all six fields with the store proven
  empty. F-31: the F-25 two-process test was probabilistic (a scheduler
  could run B to completion before A entered its window, passing
  vacuously). Rewritten as a deterministic lost-update FALSIFICATION: A
  signals READY from inside its held fence after the strict load; B signals
  STARTED at its append call; the parent then asserts B is still alive —
  blocked on the OS lock (without the fence, B completes in milliseconds
  and the assertion fails) — releases A, and both rules survive. F-32: the
  Windows CI jobs ran only the effect/recovery durability files — the
  msvcrt.locking branch of the interprocess fence was never qualified by
  CI. The windows job now also runs tests/test_user_rules.py (the full
  suite covers flock on Linux; the store suite covers msvcrt here; offline
  imports resolve through the conftest super_browser stub). F-33: the code-
  fence tolerance now accepts bare JSON or one COMPLETELY surrounding
  fence; a half-open fence (opening line, no closing marker) or a bare
  "```" rejects as unparseable_response. Evidence corrections to the
  entries below: F-25's original qualification text overstated the
  interleaving (now deterministic per F-31); F-27's "every mixed shape
  rejects" claim was true of refusal/rule tagging but not of explicit null
  in rule fields (closed by F-30). 2 new regressions; suite 1102 (count
  from the run).
- **2026-10-01 — PR #23 second review pass: F-25..F-29 (the process
  boundary, the byte boundary, and two strictness completions).** F-25
  (blocker): the F-19 mutation lock was process-local — two PROCESSES could
  still interleave read-modify-write windows and silently drop a NEVER
  (atomic os.replace serializes each install, not the read-write around
  it). Fixed: every rule-store mutation now takes a two-level fence in one
  order — the in-process per-path RLock, then an OS-RELEASED interprocess
  lock file (<store>.lock via flock/msvcrt.locking; the lock dies with the
  process, so a crashed writer cannot leave a stale lock). save() and
  append_strict() share the fence (save factored to _write_replaced);
  enforcement reads stay lock-free. Qualified with two REAL processes:
  process A holds its append window open (slow strict load), process B
  appends inside it, both rules survive. F-26 (blocker): read_text's
  UnicodeDecodeError escaped both readers' except tuples — invalid UTF-8
  bytes raised out of the enforcement path instead of voiding to zero
  rules. Both paths now treat undecodable bytes as corrupt content:
  load() → zero rules (no raise); _load_strict() → RuleStoreError, bytes
  untouched. Regression writes real invalid bytes (b"\\xff\\xfe...").
  F-27: the model output is now TAGGED, not merged — refusal = expressible
  false + non-empty string explanation + NO rule fields; rule = no
  refusal fields at all. Every mixed shape (explanation floating through
  rule fields, expressible beside a decision, refusal without its
  explanation) rejects as invalid_shape; only the two pure shapes
  interpret. F-28: empty RiskMeta.target_types is UNKNOWN vocabulary and
  can never ESTABLISH satisfiability — a selector naming a custom action
  (pre-Layer-3 constructor shape) plus any target type now rejects; naming
  the action without a target claim remains expressible. F-29: the F-08
  serialization rewrite kept, plus a NEW direct regression locking the
  unique-staging invariant itself — two saves' os.replace source paths are
  recorded and must be two distinct rules.json.<32-hex>.tmp files (never a
  shared rules.json.tmp), a check the serialized concurrency test can no
  longer provide. 5 new regressions; suite 1100.
- **2026-10-01 — PR #23 review fixes: F-19..F-24 (persistence boundary,
  strict model output, exact confirmation).** F-19 (blocker): confirm()'s
  load-then-save made enforcement's fail-closed load() ([] on corrupt
  content) indistinguishable from a genuinely empty store — confirming a
  new ALLOW against a corrupted document would atomically erase a persisted
  NEVER and install the ALLOW; the unlocked read-modify-write also admitted
  a lost-update form. Fixed in the STORE, not the compiler: RuleStore gains
  _load_strict (missing = empty; corrupt/unreadable RAISES) and
  append_strict (serialized same-path read-modify-write under a
  process-wide per-path lock — the EffectLedger idiom; save() takes the
  same lock; duplicates refuse). Regressions: corrupted NEVER+broken-entry
  document refuses mutation with bytes byte-for-byte unchanged; a same-path
  writer blocks while an append holds the lock (barrier test). F-20
  (blocker): persisted TTL now begins at CONFIRMATION time
  (UserRule.create(now=self._clock())); compiled_at stays draft/audit
  metadata — a 60s rule confirmed two minutes late is born alive, not
  dead-on-arrival. F-21: strict JSON loader (duplicate keys rejected,
  NaN/Infinity literals rejected via parse_constant, 1e999→inf caught by
  isfinite); expressible/explanation type-checked when present ("false"/0/
  null are malformed drafts, never refusals-turned-rules; explanation null
  or non-string or blank → invalid_shape); ttl finite/positive/≤max with
  the omitted-TTL case clamped to min(default, configured max); the max
  itself validated at construction. F-22: RiskMeta gains target_types (the
  registry — the layer that owns action vocabulary — now records each
  action's composed target types: like→post, post→none, follow→account,
  …), and the compiler rejects selectors PROVABLY unable to match any
  registered action (like+target "tweet"; like+tier private_reversible —
  tier derived from the same registry the gate uses). F-23: the
  owner-facing description is now canonical and lossless — sorted
  quoted/escaped arrays (["a", "b"] vs ["a or b"]), exact TTL seconds
  beside a friendly duration (86400 vs 86401 differ), and ceiling-aware
  ALLOW semantics ("will still ASK: every named tier is above the allow
  ceiling"); confirm() REVALIDATES the public draft dataclass (decision
  enum, ttl contract, satisfiability, description == canonical
  re-derivation) before touching the store. F-24: compiler_api_key is
  repr=False in WebWireConfig (regression: the secret never appears in
  repr(config); the field stays readable). 15 new regressions; suite 1095.
- **2026-10-01 — PR #22 MERGED (6653fdf) + M8 LAYER 3: the rule compiler
  (PR pending).** Layer 2 cleared the maintainer pass at 46e1227 (F-16/
  F-17/F-18 confirmed; CI 1063 total tests — 1057 pass + 6 platform-skipped
  on Linux, Windows jobs separately green) and squash-merged. Layer 3
  implemented per frozen spec section 7 (m8_compiler.py): natural language
  in; strict deterministic reduction to the frozen RuleSelector contract
  out; inexpressible clauses REJECTED with an explanation and NOTHING
  stored (M8-T12 — model refusal shape, unknown action/tier vocabulary,
  blanket no-scope rules, bad decision/ttl/shape, unparseable prose);
  unknown fields reject (strict draft schema; one mechanical tolerance — a
  surrounding code fence is stripped before strict JSON). The owner
  confirms the COMPILATION, not the words: describe_compiled_rule()
  re-expresses the structure in plain words generated by code (two
  sentences compiling to the same selector produce the identical
  description — regression-locked), compile() stores nothing, confirm() is
  the only persisting call (single-use via rule_id guard; preserves
  existing hand-written rules; provenance="compiled", source_text kept).
  Pluggable + optional: one-method CompilerModel protocol; built-in
  ApiKeyChatModel is a plain API key against a chat-completions-style
  endpoint (stdlib transport, injectable for tests; WebWireConfig carries
  compiler_api_key/compiler_endpoint/compiler_model_name, all defaulting
  off — no model configured means hand-written rules work unchanged, and
  compile() raises CompilerUnavailable). Compile-time only: a
  subprocess-level regression proves importing the store, kernel, or
  dispatcher never pulls the compiler module into the interpreter. A
  compiled+confirmed NEVER enforces through the kernel gate like any
  hand-written rule (cites the compiled rule id). 17 new tests.
- **2026-10-01 — PR #22 second review pass: F-16/F-17/F-18.** F-16 (blocker):
  layer 2 had re-imposed a stricter id contract than the frozen layer-1
  UserRule contract (any non-empty string), so a legal rule id with internal
  whitespace became an invalid approver at the mint boundary. Fixed with ONE
  invariant: user_rules.validate_rule_id is now the single rule-id authority
  (UserRule construction and validate_approver both call it); spaced ids such
  as "rule:allow likes" are legal attribution, cross-layer regression locks
  the full lineage (store round-trip → kernel attribution → mint → durable
  ledger). F-17: the acceptance tests rewritten to the scenarios the frozen
  matrix names — T20/T21 now mutate the LIVE shared registry / the gateway
  epoch after mint and deny on the actual commit path (scope_effect) with
  policy_mismatch / epoch_mismatch, and the epoch denial is terminal (the
  grant is REVOKED; every later attempt denies grant_not_active); T22
  requires BOTH the durable RESERVED reservation and the terminal
  confirmation (the like policy is fenced — ReplaySemantics.UNKNOWN); T24
  crosses actual kernel human confirmation (token issued and consumed) →
  adapter → runtime → permit → ledger; T16 compares the full response
  byte-for-byte (only the volatile token mint fields normalized); the
  invalid-approver ledger parse regression uses a canonical UPPERCASE state
  so the approver vocabulary is provably the rejection. F-18: the five
  non-engagement adapters no longer park the approver in mutable instance
  state (_m8_approver removed) — execute() delegates to
  execute_with_approver(approver="human") exactly like the engagement
  adapter; approval provenance is per-call throughout.
- **2026-10-01 — M8 LAYER 2 (PR #22): the kernel rule gate + approver
  lineage.** Implemented per the corrected section 6 topology: the gate sits
  after preview and before the confirmation section, so it is re-evaluated
  on EVERY invocation including the token-bearing one (T14: a NEVER
  installed after token issuance still denies — the gate dominates tokens).
  NEVER denies with blocked_by=user_rule and the rule cited; ASK/no-match
  uses the existing human confirmation path with the matched rule cited in
  the card payload; below-ceiling ALLOW establishes approver="rule:<id>"
  with NO confirmation carrier and flows to execution in ONE invocation.
  The critical rule honored: approval source is an explicit trusted value —
  the kernel passes approver through a new execute_with_approver adapter
  seam, the six M5 executors thread it to M5ExecutionRuntime.issue, and the
  runtime stamps it on the grant at the SINGLE mint seam. Capabilities
  without the seam are denied standing approval
  (approver_unsupported_adapter). Attribution descends monotonically:
  ApprovalGrant.approver -> EffectPermit.approver -> EffectLedgerRecord
  .approver (top-level lineage field; optional on read for pre-M8 rows —
  T23 locks old rows parse with None). Human default "human" keeps every
  pre-M8 caller correct (T24). Also landed: the reviewer's two cleanups
  (section 6.1 typo; the Windows retry now inspects winerror 5/32, retrying
  only the transient sharing violation per the stated contract). T13-T24
  all covered (tests/test_m8_rule_gate.py, 13 tests). Suite 1057 (count
  from the run). Kernel with rule_store=None is byte-for-byte pre-M8 (T16).
- **2026-10-01 — M8 layer 1 hardening + section 6 amendment (PR #21).** The
  post-merge fallback pass found one new blocker and one falsified design
  assumption. F-08 (blocker): concurrent saves shared one staging file —
  writer A's os.replace could install writer B's bytes while A reported
  success (believing a restrictive policy installed while a permissive one
  persisted). Each save now stages through a unique per-writer temp file;
  a bounded retry absorbs the Windows transient sharing violation on the
  destination; a deterministic barrier-based concurrency test proves every
  successful replace installs that caller's payload (run 5x for flake).
  Hardening: selector annotations became runtime contracts (exact non-empty
  frozensets of the exact element type — strings/RiskTier enums; plain
  strings, mutable sets, empty dimensions, empty-string elements all
  rejected); bool timestamps rejected at construction (parity with the
  parser — bool is numeric in Python). Doc repair: target_types added to
  the frozen selector diagram. SECTION 6 REWRITTEN per the corrected
  authority topology: the kernel does NOT mint ApprovalGrants — the
  execution runtime is the single mint seam; rule-ALLOW establishes
  approver attribution with no human carrier; the rule gate is
  re-evaluated on every invocation including the token invocation (a new
  NEVER defeats an old human token); attribution descends
  grant→permit→ledger with approver as a top-level ledger lineage field
  (optional on read for pre-M8 rows). Acceptance matrix extended
  M8-T13..T24. Suite 1044 (count from the run).
- **2026-10-01 — M8 layer 1 review fixes (PR #20, fallback pass).** The
  external reviewer quota was unavailable, so the project's fallback rule
  applied: a first-pass plus adversarial review, seven findings (F-01..F-07,
  three blocking), every one verified against the code before fixing.
  F-01 (blocker): invalid-entry skipping could WIDEN authority — a broken
  NEVER left a valid ALLOW live; the old T7d tested only the safe direction.
  Now any invalid entry voids the entire read to zero rules. F-02 (blocker):
  enforcement caching could serve revoked authority; the cache is removed —
  every match reads current persisted policy, making revocation freshness a
  store invariant rather than integrator discipline. F-03 (blocker): NaN/±inf
  timestamps (JSON accepts them as literals) made rules effectively
  non-expiring; TTLs now finite and strictly forward, both at construction
  and parse. F-04: save() raises RuleStoreError instead of swallowing write
  failures. F-05: strict parse — provenance and source_text mandatory,
  never manufactured. F-06: the frozen matcher signature (with target_type)
  implemented and test-locked; target_types selector dimension added.
  F-07: rule ids non-empty and unique; save() refuses duplicates. The
  review-required test list is implemented in full, including the widening
  case (malformed NEVER + valid ALLOW → no auto-approval) and both cache
  revocation directions. Suite 1034 (count from the run).
- **2026-10-01 — M8 design FROZEN + layer 1 (PR #20).** M8 adds the user
  rule layer: standing decisions in the owner's own words, compiled once to
  structured selectors, enforced deterministically at the approval gate.
  docs/M8_DESIGN.md is normative and self-contained (four layers; negative
  guarantees; M8-T1..T12 acceptance table). Two standing decisions recorded:
  the compile-time model uses a plain API key (pluggable, optional —
  hand-written rules need no model); no model ever runs in the enforcement
  path. Layer 1 (this PR): RuleSelector (every specified dimension must
  match; empty scope rejected at construction), UserRule with mandatory TTL
  and provenance, deterministic matcher with never>ask>allow precedence,
  the risk-tier ceiling enforced as a MATCH-TIME downgrade (an ALLOW match
  above the ceiling yields ask — no store content can bypass it), and a
  file-backed store with atomic writes and fail-open-equals-everything-asks
  semantics (missing/corrupt/schema-mismatch → zero rules; one invalid entry
  skipped without disabling the owner's bans). 18 tests (M8-T1..T9 covered).
  Suite 1008 (count from the run). House rule adopted 2026-10-01: repository
  content names no external products — reviews are "external review,"
  patterns are described generically.
- **2026-09-28 — M7 Layer 1 owner-lock candidate (PR #19).** Started from the
  exact M7-design merge `65e3961ed32d090b6a47983901d153838cf99267`.
  Maintainer-first review froze the canonical-domain/process-registry/OS-lock
  boundary before independent review. The implementation adds
  `AuthorityOwnerLock`, absolute domain freezing, normalized same-process
  exclusion, non-blocking POSIX/Windows lock adapters, non-inheritable private
  descriptors, a POSIX after-fork child detach, stable non-authoritative
  rendezvous-file behavior, and genuine fresh-process exclusion/successor tests.
  The maintainer adversarial re-open found two lifecycle defects before independent external review:
  explicit unlock-before-close violated M7-RV17's owner-handle lifetime law, and
  registry validation occurred too late after OS release. Both were corrected:
  release is now close-only and registry authority is verified/pinned across the
  close boundary. Initial Windows CI then caught a test-proof defect: a
  `Path.exists()` case-alias assertion is meaningless on a real case-insensitive
  filesystem. The test now proves the intended invariant directly by asserting
  that duplicate normalized-domain admission is denied before the second OS
  lock-file open. Layer 1 remains mechanism-only until Layer 2 runtime integration.
- **2026-09-28 — M7 normative design merged (PR #18).** Reviewed candidate
  `73993c54949990b81e598693f730bacf18665f89` and squash merge
  `65e3961ed32d090b6a47983901d153838cf99267` share tree
  `bdb4a866738735900c14969b56475cb0b905571d`. Maintainer-first review,
  adversarial re-opens, and independent external-review reconciliation produced RV01–RV18
  and a 70-case primary acceptance surface. The bounded topology is one
  non-expiring qualified owner per canonical local state directory, local IPC
  clients, no timed live-owner stealing, and no automatic mutation replay across
  owner-instance replacement. Media-backed writes are withheld until owner-side
  artifact ingress is qualified.
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
  external-review action was available because the automation quota was
  exhausted, so the frozen-design fallback was a distinct recorded
  adversarial second pass with zero additional production findings.
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
  78 source files. An external exact-head review was unavailable because the repository
  code-review usage limit was exhausted; no external review appeared. Per the
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
  3.11/3.12 with 763 tests, Ruff and mypy. Exact-head external review completed
  with zero findings. Maintainer-first review had already discovered/fixed three
  authority defects: stale concurrent RecoveryGuard publication, caller-
  controllable recovery exemption, and cross-capability confirmation-token
  transfer. Squash merge: `03df4d3458828cc131989fd44fe27d48cef1240c`.
- **2026-09-25 — M5 L5 merged (PR #6).** Final candidate
  `7885de6abd385c7da25b0ac61d61627d2f465c54`; CI #353 green on Python
  3.11/3.12 with 749 tests, Ruff and mypy. Exact-head external coverage was
  incomplete because of its bundle line limit; its three findings were
  independently reconciled. An exact-head external rerun was unavailable because the
  repository review quota was exhausted. Squash merge:
  `515999740bb7b5fa4ad9c74582d6166157e9075b`.
- **2026-09-25 — M5 L4 already merged.** Scoped-authority baseline merged as
  `5650982b718f78c8aa10709cdb9f257e73374ce2`; Layer 5 was built from that
  exact main baseline.
- Detailed project history through the Layer-3 close-out on 2026-09-24 is
  preserved verbatim in `docs/STATE_HISTORY_THROUGH_2026-09-24.md`.