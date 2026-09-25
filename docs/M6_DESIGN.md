# M6 — Evidence-Bearing Reconciliation & Qualification Boundary

```text
Status:   CANDIDATE — MAINTAINER-FIRST REVIEW IN PROGRESS
Base:     main 666064b3c5dc3f905be11321a3604460410dd583
Runtime:  M5 baseline 0c62402ae01b50d7662b3978cbf2bee4109aa035
Scope:    reconcile durable M5 uncertainty without rewriting history;
          qualify durability/replay claims that remain intentionally bounded
Change rule: revise only when implementation, fault injection, live evidence,
platform evidence, or an independently verified review finding falsifies an
invariant or assumption.
```

This document is normative and self-contained for M6. Build M6 from this file,
not from review-conversation memory. `docs/M5_DESIGN.md` remains authoritative
for the effect transaction boundary and is not reopened by M6 unless evidence
falsifies an M5 invariant.

M6 begins from a completed M5 runtime with these established facts:

- `EffectLedger` is the fsync-backed source of durable M5 effect facts;
- `EFFECT_UNKNOWN` is terminal historical effect knowledge and cannot be
  overwritten;
- raw `RESERVED` projects to effective unknown after restart;
- `RecoveryGuard` blocks matching semantic replay while durable uncertainty is
  unresolved and fails closed when recovery truth cannot be established;
- `ApprovalGrant` and `EffectPermit` are intentionally ephemeral execution
  authority and are never reconstructed from durable evidence;
- the invocation journal is audit-only and is never a mutation or recovery
  authority source;
- the supported runtime remains single-process; cross-process writer/browser
  coordination is a later milestone.

M6 adds a second orthogonal durable axis:

```text
historical effect fact  +  evidence-bearing reconciliation fact
        EffectLedger              ReconciliationLedger
                \                    /
                 \                  /
                  -> RecoveryProjector -> RecoveryGuard
```

The governing rule is:

> **Historical uncertainty is immutable. Reconciliation adds evidence; it does
> not rewrite the original effect fact. Reconciliation may clear one recovery
> uncertainty gate for a future invocation, but it never restores old execution
> authority or bypasses any other policy gate.**

---

## 1. Problem statement

M5 deliberately stops automatic replay when a REQUIRED effect is durably
uncertain. That is correct fail-closed behavior, but uncertainty needs an
operator-controlled way to be resolved later.

Two raw M5 states are recovery-unresolved:

```text
RESERVED         # after restart, mutation may or may not have crossed
EFFECT_UNKNOWN   # runtime explicitly could not establish terminal effect truth
```

Today `RecoveryGuard` can only answer:

```text
unresolved -> block
otherwise  -> clear
```

It has no evidence-bearing way to state that a previously uncertain effect was
later established to have occurred or established not to have occurred.

M6 introduces that boundary while preserving the M5 laws that made uncertainty
safe.

### 1.1 Dangerous shortcuts M6 rejects

M6 must not:

- mutate `EFFECT_UNKNOWN` into `EFFECT_CONFIRMED` or `NO_EFFECT`;
- interpret a failed browser read, missing selector, timeout, 404, or empty
  result as proof of no external effect;
- let an AI/model/evidence collector clear a recovery block merely because it
  produced a plausible explanation;
- persist or revive an old ApprovalGrant, EffectPermit, claim, or confirmation
  token after reconciliation;
- treat a best-effort invocation-journal row as reconciliation evidence or
  recovery authority;
- silently choose the latest of contradictory reconciliation facts;
- infer that matching content/state proves causal identity when the evidence
  cannot distinguish this attempt from another external action;
- trust a reconciliation row whose append reported durability failure merely
  because its bytes are visible;
- expose a newly durable reconciliation to a write invocation before stale
  confirmation authority has been invalidated;
- reset dedupe/rate/kill/policy state merely because recovery uncertainty was
  reconciled;
- rotate/delete reconciliation safety facts using audit-journal retention rules;
- solve cross-process coordination incidentally inside reconciliation.

---

## 2. Scope and non-goals

### 2.1 In scope

M6 defines and qualifies:

1. a durable `ReconciliationLedger` separate from `EffectLedger`;
2. exact lineage binding from reconciliation facts to one M5 `effect_id`;
3. evidence and claim semantics for terminal reconciliation;
4. explicit human/operator reconciliation authority;
5. a composite `RecoveryProjector` joining effect and reconciliation truth;
6. `RecoveryGuard` behavior after terminal reconciliation;
7. a process-local reconciliation publication fence;
8. crash, corruption, restart, ambiguity, and exact-fact retry semantics;
9. a narrow local operator recovery workflow;
10. stale-confirmation invalidation across a reconciliation boundary;
11. platform qualification of durability claims, especially Windows;
12. evidence-driven replay-safety qualification such as like/unlike without
    presuming a policy upgrade.

### 2.2 Out of scope

M6 does **not** provide:

- distributed or cross-process exactly-once execution;
- cross-process EffectLedger/ReconciliationLedger writer coordination;
- a remote reconciliation service;
- automatic model-authorized terminal resolution;
- automatic terminal reconciliation from browser heuristics;
- persisted ApprovalGrant or EffectPermit authority;
- a generic workflow engine or general CLI framework;
- multi-user / role-based authorization;
- durable cross-restart rate limits or semantic dedupe for confirmed effects;
- a policy upgrade for like/unlike unless concrete broker evidence earns it;
- cryptographic tamper evidence against a hostile local filesystem user;
- correction/supersession of a terminal reconciliation verdict;
- rewriting or deletion of historical M5 effect facts.

M6 inherits M5's trusted local state-directory and single-process assumptions.
Cross-process coordination is a separate M7 design problem.

A mistaken terminal operator reconciliation is intentionally not silently
editable in M6. Corrective/superseding reconciliation would need explicit
history semantics and is a future design problem.

---

## 3. Constitutional distinctions

M6 preserves:

```text
observation        != evidence claim
claim              != reconciliation authority
effect history     != reconciliation history
reconciliation     != old-attempt continuation
recovery clear     != all policy gates clear
eligibility        != execution permission
semantic equality  != causal identity
storage identity   != semantic identity
absence            != proven no-effect
visible bytes      != known durable fact
durable fact       != published recovery authority
UNKNOWN            != NO_EFFECT
```

Project-wide evidence laws apply:

- **Evidence ≠ claim.** Observation supports a bounded statement; it does not
  automatically authorize a verdict.
- **Cognition ≠ authority.** An agent/model may collect, summarize, or propose;
  it does not commit terminal recovery truth.
- **Execution ≠ external effect.** A successful call path does not establish
  remote effect occurrence.
- **Verification ≠ generalization.** A proof predicate is valid only for the
  action/environment/evidence class actually qualified.

---

## 4. Terminology

| Term | Meaning |
|---|---|
| **Unresolved effect** | An EffectLedger effect whose raw latest state is `RESERVED` or `EFFECT_UNKNOWN`. |
| **Reconciliation evidence** | Structured observations presented as basis for an operator decision. Evidence is not authority. |
| **Reconciliation verdict** | One durable operator-authorized terminal statement: `CONFIRMED_EFFECT` or `CONFIRMED_NO_EFFECT`. |
| **ReconciliationLedger** | Fsync-backed append-only safety ledger at `.webwire/reconciliations.ndjson`. |
| **RecoveryProjector** | Validated join of EffectLedger and ReconciliationLedger into current recovery truth. |
| **RecoveryGuard** | Process-local enforcement cache/gate derived from the composite projection. |
| **ReconciliationAuthority** | Narrow local authority bound to one immutable reconciliation fact after explicit operator confirmation. |
| **EvidenceCollector** | Read-only mechanism that gathers observations and may propose; it cannot commit a verdict. |
| **Fresh invocation** | A new normal capability execution with new human confirmation and new M5 grant/attempt lineage. |
| **Live attempt ownership** | A current in-process M5 attempt still capable of writing a terminal outcome for the target effect. |
| **Durability-ambiguous reconciliation** | Complete row may be visible after append reported durability failure; it is not recovery authority until re-durability succeeds. |
| **Reconciliation publication fence** | Process-local fence shared by terminal reconciliation and authoritative RecoveryGuard refresh so newly durable clear truth cannot be observed before stale confirmation invalidation. |
| **Confirmation invalidation boundary** | Successful durable terminal reconciliation invalidates all pending pre-resolution WriteKernel confirmation tokens before clear truth becomes observable to writes. |

---

## 5. Separate reconciliation history

M6 does not add successors to M5 `EffectState`:

```text
RESERVED -> NO_EFFECT | EFFECT_CONFIRMED | EFFECT_UNKNOWN
EFFECT_UNKNOWN is terminal
```

Reconciliation asks:

```text
What later evidence justifies resolving operational uncertainty created by that
immutable historical fact?
```

Therefore:

```text
EffectLedger
  effect_id = E
  state = EFFECT_UNKNOWN
  ... immutable M5 lineage ...

ReconciliationLedger
  effect_id = E
  verdict = CONFIRMED_EFFECT | CONFIRMED_NO_EFFECT
  ... exact copied lineage + evidence + operator authority ...
```

This ensures M5 history stays schema-stable, reconciliation cannot masquerade as
execution outcome, evidence provenance is distinct, and recovery is recomputed
from append-only histories.

The invocation journal is neither ledger.

---

## 6. Reconciliation verdicts

Exactly two terminal verdicts exist:

```text
CONFIRMED_EFFECT
CONFIRMED_NO_EFFECT
```

There is intentionally **no durable `INCONCLUSIVE` resolution state**.
Inconclusive inspection appends no recovery-authoritative fact; audit may record
it while RecoveryGuard remains blocking.

### 6.1 `CONFIRMED_EFFECT`

Later evidence plus explicit operator authority establish, within the stated
evidence boundary, that the uncertain external effect should be treated as
having occurred.

Consequences:

- EffectLedger history remains unchanged;
- this effect no longer contributes an unresolved recovery block;
- original attempt is never resumed;
- old approval/permit/confirmation authority is not restored;
- later identical semantic action still traverses the ordinary policy pipeline
  and requires new human confirmation;
- M6 creates no permanent cross-restart dedupe ban for confirmed effects.

This mirrors ordinary M5 `EFFECT_CONFIRMED`: known historical occurrence is not
an indefinite ban on future newly approved identical intent.

### 6.2 `CONFIRMED_NO_EFFECT`

Later evidence plus explicit operator authority establish, within the stated
evidence boundary, that the uncertain attempt did not produce the approved
external effect.

History stays immutable; the effect's recovery uncertainty contribution is
removed; old authority remains dead; any later mutation is fresh.

### 6.3 Reconciliation clears only recovery uncertainty

Terminal reconciliation does **not** guarantee immediate executability.
Independent gates remain authoritative:

```text
kill switch / authorization epoch
risk + policy registry
actor/target/intent bindings
process-local token bucket
process-local semantic dedupe
fresh human confirmation
M5 grant/permit rules
```

M6 does not remove a DedupeStore entry or refund TokenBucket capacity. A
`CONFIRMED_NO_EFFECT` may therefore remain temporarily blocked by process-local
defense-in-depth state. That is not a recovery inconsistency.

### 6.4 Absence is not automatically `CONFIRMED_NO_EFFECT`

Insufficient on their own:

```text
selector missing
navigation/read timeout
remote 404 or transient error
empty search result
current object not visible
current state differs from desired state
failed verification call
process crash
```

Negative proof requires an accepted action-specific conclusive predicate or
explicit operator decision grounded in preserved evidence. M6 initially defines
no generic automatic negative-proof predicate.

---

## 7. Reconciliation record schema

```text
ReconciliationRecord
  reconciliation_id: non-empty string
  effect_id:          non-empty string

  # copied immutable M5 lineage
  semantic_key:       non-empty string
  action_type:        non-empty string
  intent_hash:        non-empty string
  policy_binding:     non-empty string
  actor_id:           optional non-empty string
  target_type:        optional non-empty string
  target_id:          optional non-empty string

  verdict:            CONFIRMED_EFFECT | CONFIRMED_NO_EFFECT

  operator_id:        non-empty string
  evidence_hash:      lowercase SHA-256 hex of canonical evidence JSON
  evidence:           non-empty strict-JSON object
  timestamp:          UTC wall-clock provenance string
```

### 7.1 Lineage equality

Every copied lineage field exactly equals canonical first EffectLedger lineage
for `effect_id`. Target history must currently be raw:

```text
RESERVED | EFFECT_UNKNOWN
```

Unknown effects, settled M5 effects, or changed lineage are invalid targets.

### 7.2 Evidence and canonical hash

Evidence uses M5 strict portable JSON. Minimum:

```text
basis:          non-empty project-defined string
observed_at:    UTC provenance string
observations:   non-empty list of structured observations
```

`evidence_hash` uses canonical JSON:

```text
UTF-8
sorted object keys
compact separators
finite JSON numbers only
no non-JSON coercion
SHA-256 -> lowercase hexadecimal
```

Hash mismatch invalidates the record. ReconciliationAuthority binds this hash.

External artifacts carry content identity:

```text
artifact:
  kind: ...
  sha256: ...
  location: optional diagnostic locator
```

Path/URL alone is not evidence identity.

Evidence is minimized to facts needed for the bounded claim. Raw page dumps,
cookies, private timelines, or unrelated DOM content are not stored by default.
Sensitive artifacts remain outside the ledger and are digest-referenced when
necessary.

### 7.3 Operator identity

`operator_id` identifies local human/operator authority, not X `actor_id`.
Under M6's local single-user model it is provenance, not RBAC.

---

## 8. ReconciliationLedger contract

For one effect:

```text
no reconciliation -> one terminal reconciliation verdict
terminal reconciliation -> no successor
```

A second different verdict is corruption, not supersession or last-row-wins.

Exact retry may re-establish durability without duplicate. Exactness includes:

```text
reconciliation_id
effect_id
all copied lineage
verdict
operator_id
evidence_hash
evidence
```

Timestamp may differ only for exact-fact re-durability comparison.

### 8.1 Durability

Uses the M5 safety posture:

- append-only NDJSON;
- owner-writable state file;
- full write loop;
- file fsync before success;
- parent-directory fsync for new entries where exposed;
- process-local same-path writer serialization;
- schema/history corruption fails closed;
- exact-fact re-durability after ambiguous append failure.

### 8.2 Durability-ambiguity latch

Trusting visible but not known-durable `CONFIRMED_NO_EFFECT` could remove a
safety block. Visibility cannot stand in for durability.

For each normalized reconciliation-ledger path, the process shares ambiguity
state for a fact whose append may have written bytes but did not report complete
file + required directory durability.

```text
append reports ambiguity
→ exact fact locally AMBIGUOUS
→ composite projection for path unavailable/fail-closed
→ visible row MUST NOT clear RecoveryGuard
→ exact-fact re-durability re-fsyncs file/directory as applicable
→ successful re-durability clears AMBIGUOUS
→ projector may then use fact
```

Same-path instances share writer locking and ambiguity state.

After restart the latch is gone. Before startup recovery trusts an existing
reconciliation file, the supported reader establishes current file durability
with writable-handle fsync and parent-directory fsync where applicable. Failure
makes recovery unavailable.

### 8.3 Retention

`reconciliations.ndjson` is safety state, not audit output:

```text
no TTL expiry
no monthly rotation
no best-effort pruning
no independent deletion
no compaction in M6
```

A fact is retained at least as long as corresponding EffectLedger history can
participate in recovery. Future compaction/archival must preserve joined semantic
history atomically and is outside M6.

Missing reconciliation file is valid empty history and can only conservatively
re-block unresolved effects. A reconciliation record whose target effect is
missing from EffectLedger is corruption/fail-closed.

---

## 9. Evidence semantics and claim ceiling

Evidence must support the claim about the specific effect. Plausible state match
is not causal proof.

```text
matching post text exists
    != this exact unknown attempt created it
object currently absent
    != this exact attempt produced no effect
current bookmark state is set
    != proof of which actor/process set it
```

Action-specific policy may prove more only after the predicate is explicitly
designed and regression-qualified.

### 9.1 Positive evidence

Positive remote identity can be strong when uniquely bound to approved lineage:
for example exact remote object identity plus actor/target/content proof under a
qualified action-specific reconciler.

If observation cannot distinguish this attempt from pre-existing/external
equivalent state, collector reports ambiguity rather than causal identity.

### 9.2 Negative evidence

Negative evidence has a higher burden. Read failure is not absence; observed
absence is not automatically proof that no effect ever occurred.

`CONFIRMED_NO_EFFECT` requires either an accepted action-specific conclusive
predicate or explicit operator resolution preserving basis/limitations.

### 9.3 Evidence collector boundary

EvidenceCollector may read canonical lineage, perform bounded read-only remote
inspection, produce structured observations, and suggest a non-authoritative
verdict.

It may not append reconciliation, clear guard, construct execution authority,
mutate external state, or infer no-effect from read failure.

Browser-backed inspection uses the same M5 leased-read/browser-state coordination
as ordinary evidence reads. If a content owner holds browser state, inspection
returns busy/inconclusive and does not navigate across the owner.

---

## 10. Reconciliation authority

Clearing recovery uncertainty is safety-relevant authority. It is not a normal
capability write and does not reuse WriteKernel confirmation authority.

ReconciliationAuthority is local/operator-facing, absent from normal capability
registration, has no raw mutation surface, and is created only after explicit
human confirmation of effect, verdict, evidence hash, and evidence summary.

It binds exactly:

```text
effect_id
verdict
evidence_hash
operator_id
```

It cannot mint ApprovalGrant/EffectPermit or mutate EffectLedger; it may only
request persistence of that bound reconciliation fact.

Programmatic non-interactive model-authorized terminal reconciliation is out of
scope.

### 10.1 One authority, one fact, possibly multiple durability attempts

Single-use means one immutable **fact**, not one filesystem syscall.

Before first durability I/O the coordinator freezes the complete record and
checks evidence hash against approved authority. If persistence reports
ambiguity, the same authority lineage may re-drive only the exact frozen fact.
Verdict, evidence, lineage, and operator identity cannot change.

After known durable success authority is consumed permanently.

### 10.2 Recovery-only, not in-flight rescue

Reconciliation may not race a live M5 attempt capable of writing terminal
EffectLedger outcome for the same effect.

```text
live nonterminal attempt owns effect_id
    -> denied: live_attempt_owned
```

The check is against canonical CommitGateway/attempt lifecycle ownership under a
lifecycle fence, not a best-effort diagnostic list. Once the check reports no
live owner, that effect cannot later reacquire an old in-process attempt.

After restart ephemeral attempts/grants are gone; stale durable `RESERVED` is
eligible under the normal single-process assumption.

### 10.3 Kill-switch relationship

Kill blocks external capability execution, not local read-only inspection or
durable reconciliation bookkeeping. Reconciliation does not reset kill state,
decrement authorization epoch, or mint authority. Future mutation remains
subject to current kill/epoch gates.

---

## 11. Reconciliation publication fence

A durable reconciliation row becomes filesystem-visible before all in-memory
post-conditions are necessarily complete. A concurrent write invocation must not
observe newly clear recovery truth while a pre-resolution confirmation token is
still valid.

M6 therefore introduces one process-local **ReconciliationPublicationFence**
shared by:

- terminal ReconciliationCoordinator resolution;
- authoritative composite RecoveryProjector/RecoveryGuard refresh used by write
  enforcement.

Raw diagnostics may read files without this fence, but they are not authority.

WriteKernel confirmation-token issue/validate/consume/invalidate operations also
use a synchronized token-store fence. Terminal reconciliation uses the
publication fence first, then the token-store fence for invalidation.

Authoritative lock order:

```text
ReconciliationPublicationFence
  -> ReconciliationCoordinator protocol state
    -> ledger/path locks as required
    -> WriteKernel confirmation-token store fence (for invalidation)
    -> RecoveryGuard publication/cache lock
```

Implementations may collapse adjacent internal locks but must preserve the same
observable ordering and avoid reverse acquisition.

A write-side RecoveryGuard refresh acquires the publication fence before reading
composite recovery truth. Therefore it either:

1. observes the old unresolved state before reconciliation wins; or
2. waits until durable reconciliation + pending-token invalidation are complete,
   then observes clear truth.

There is no state in which the write path can observe reconciliation-clear while
pre-resolution confirmation authority is still valid.

---

## 12. Reconciliation protocol

Conceptually:

```text
resolve(effect_id, verdict, evidence, operator_authority)
```

Required ordering:

```text
acquire ReconciliationPublicationFence
→ acquire reconciliation protocol state/fence
→ verify no live nonterminal M5 attempt owns effect_id
→ fully read/validate EffectLedger
→ require target raw RESERVED | EFFECT_UNKNOWN
→ capture exact immutable effect lineage
→ canonicalize evidence + verify evidence_hash
→ fully read/validate/re-durable ReconciliationLedger
→ require no terminal reconciliation exists
→ validate operator-authority binding
→ freeze complete ReconciliationRecord
→ append exact record
→ fsync reconciliation file (+ required parent durability)
→ clear local ambiguity latch only after durability success
→ consume reconciliation authority for frozen fact
→ synchronously invalidate all pending pre-resolution WriteKernel confirmation
  tokens in active supported runtime
→ refresh/publish composite RecoveryGuard state while publication fence is held
→ release publication fence
→ return durable reconciliation result
```

If no active Dispatcher/WriteKernel exists, there are no supported pending
confirmation tokens to invalidate; restart/standalone recovery already begins
with an empty token store.

If append reports ambiguity, frozen fact/bounded authority lineage remains only
for exact re-durability. Publication fence is released with recovery still
blocked/unavailable; no clear state is published.

### 12.1 Why pending confirmations are invalidated

Current confirmation tokens are in-memory, capability/intent-bound, single-use,
and time-limited. A token can remain unconsumed while RecoveryGuard denies its
second phase. If reconciliation clears guard, that old token would otherwise
cross without new post-reconciliation human confirmation.

Durable terminal reconciliation is therefore a **global pending-confirmation
invalidation boundary for the active supported runtime**. Invalidating unrelated
pending tokens is conservative and intentional.

### 12.2 Confirmation token synchronization

M6 requires token issuance, validation/consume, and global invalidation to be
synchronized. A token racing invalidation has one ordering:

- token operation wins before invalidation; or
- invalidation wins and token is rejected.

For a semantic key that is still recovery-blocked, a token cannot legitimately
cross the RecoveryGuard boundary before reconciliation publication completes.

### 12.3 Concurrency

Within the supported process:

- reconciliation operations serialize;
- competing terminal verdicts yield at most one durable winner;
- terminal M5 outcome writing and reconciliation do not race a live target;
- authoritative guard refresh is publication-fenced;
- complete joined snapshots only are published;
- older clear snapshots never overwrite newer blocked/unavailable truth;
- same-path reconciliation instances share writer lock + ambiguity state.

Independent external processes remain out of scope until M7.

---

## 13. Composite recovery projection

M6 enforcement joins both histories:

```text
EffectLedger -----------\
                         -> RecoveryProjector -> RecoveryGuard
ReconciliationLedger ---/
```

`EffectLedger.recovery_projection()` may remain an M5 diagnostic/helper; M6
enforcement uses the composite projector under the publication fence.

| Raw M5 state | Reconciliation | Composite disposition | Guard |
|---|---|---|---|
| `NO_EFFECT` | none | `SETTLED_NO_EFFECT` | recovery-clear |
| `EFFECT_CONFIRMED` | none | `SETTLED_EFFECT` | recovery-clear |
| `RESERVED` | none | `UNRESOLVED_UNKNOWN` | block |
| `EFFECT_UNKNOWN` | none | `UNRESOLVED_UNKNOWN` | block |
| `RESERVED` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | recovery-clear |
| `EFFECT_UNKNOWN` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | recovery-clear |
| `RESERVED` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | recovery-clear |
| `EFFECT_UNKNOWN` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | recovery-clear |

Impossible combinations—including reconciliation attached to unknown/already
settled M5 effect—are corruption and make recovery unavailable.

Durability-ambiguous reconciliation is not projection authority even when bytes
are visible.

### 13.1 Recovery-clear is not old-attempt replay

After either verdict:

```text
old confirmation token: invalidated/not reconstructed
old ApprovalGrant:       not reconstructed; never reopened
old EffectPermit:        not reconstructed/reused
old EffectAttempt:       historical only
```

A later mutation must traverse all current independent policy gates and obtain
new human confirmation before new M5 grant/attempt authority exists.

---

## 14. RecoveryGuard semantics after M6

1. Startup validates both safety ledgers before browser mutation is available.
2. Existing reconciliation history is re-durability-fsynced before it may clear
   recovery in a new process.
3. Every supported M5 mutation takes the publication fence and refreshes the
   composite projection before browser-capable preview.
4. If either ledger cannot be read/validated/re-durability-established, cached
   clear state is discarded and mutation fails closed.
5. Only `UNRESOLVED_UNKNOWN` contributes a recovery semantic-key block.
6. Terminal reconciliation removes only that contribution after known durability
   and pending-token invalidation.
7. Diagnostic health/status never substitutes for enforcement.
8. Invocation-journal content cannot affect recovery truth.
9. Multiple unresolved effects sharing a key keep it blocked until all are
   settled/reconciled.

---

## 15. Failure and crash semantics

### 15.1 Crash before append

No durable reconciliation; recovery remains blocked.

### 15.2 Complete bytes visible but fsync reports failure

Fact is locally durability-ambiguous. Projection unavailable; exact retry
re-establishes durability without duplicate.

### 15.3 Durable append, crash before token invalidation / guard publication

Process death discards pending confirmation tokens. Restart re-establishes
reconciliation-file durability, rebuilds composite truth, and is safe without an
in-memory post-append marker.

### 15.4 Durable append, token invalidation succeeds, guard refresh fails

Process remains fail-closed. Invalidated tokens stay invalid.

### 15.5 Corrupt ReconciliationLedger

Recovery unavailable; no EffectLedger-only fallback.

### 15.6 Corrupt EffectLedger

Existing M5 fail-closed behavior remains.

### 15.7 Invocation journal failure/corruption

No M6 authority effect.

---

## 16. Operator workflow

M6 supports a narrow local recovery workflow, not a general CLI platform.

Conceptual commands:

```text
webwire recovery list
webwire recovery show <effect_id>
webwire recovery inspect <effect_id>
webwire recovery resolve <effect_id> --verdict confirmed-effect
webwire recovery resolve <effect_id> --verdict confirmed-no-effect
```

Frozen semantics:

- `list/show/inspect` are read-only;
- browser-backed inspect uses leased-read coordination and returns busy rather
  than navigating across an owned composer;
- inspection may remain inconclusive;
- resolve presents lineage, verdict, canonical evidence hash, and summary;
- terminal resolution requires explicit interactive confirmation in same local
  session;
- no hidden non-interactive Dispatcher capability bypass;
- resolve performs no compensating remote write;
- reconciliation may complete while kill is tripped but does not reset kill;
- after resolution any mutation is a separate normal invocation.

Workflow assumes exclusive WireAgent process ownership. Concurrent independent
recovery/runtime processes are unsupported.

---

## 17. Windows durability qualification

Current durability CI evidence is Ubuntu. M6 qualifies target-platform claims.

Windows qualification covers both safety ledgers:

- state-directory/file creation behavior;
- append and writable-handle flush behavior;
- exact-fact re-durability after simulated ambiguous flush failure;
- reconciliation ambiguity-latch behavior;
- startup reconciliation-file re-durability before projection;
- truncated/corrupt tail fail-closed behavior;
- restart projection from durable files;
- parent-directory behavior according to what Python/Windows exposes;
- no stronger persistence claim than evidence supports.

Falsified assumptions revise design/runtime. Success is bounded platform
qualification, not universal filesystem proof.

---

## 18. Replay-safety qualification

M6 may investigate conservative M5 replay classifications independently from
reconciliation completion.

Like/unlike remain:

```text
ReplaySemantics.UNKNOWN
DurabilityPolicy.REQUIRED
```

until concrete broker behavior proves directional state-set semantics across at
least:

```text
unliked + like    -> exactly liked
liked   + like    -> zero mutation
liked   + unlike  -> exactly unliked
unliked + unlike  -> zero mutation
both selectors present
neither selector present
hydration/state transition
stale selector
read/navigation coordination
```

Higher-level pre-state check is insufficient. Promotion to `SAFE_STATE_SET` /
`BEST_EFFORT` requires concrete broker implementation + regression evidence.
Failed qualification is retained evidence; conservative policy stays unchanged.

---

## 19. Frozen M6 invariants

1. M5 EffectLedger history is never rewritten by reconciliation.
2. `EFFECT_UNKNOWN` remains terminal historical M5 state.
3. Reconciliation is a separate durable evidence/authority axis.
4. Only raw `RESERVED` or `EFFECT_UNKNOWN` effects are valid targets.
5. Reconciliation lineage exactly matches canonical M5 effect lineage.
6. Exactly two terminal verdicts exist.
7. Inconclusive evidence appends no recovery-authoritative terminal fact.
8. Every terminal fact carries strict evidence, canonical evidence hash, and
   explicit operator identity.
9. Evidence collectors may propose; they may not commit truth.
10. ReconciliationAuthority binds one immutable fact and may re-drive only that
    fact through durability ambiguity.
11. A live in-process attempt cannot be reconciled while it can emit M5 terminal
    outcome.
12. Known reconciliation durability precedes recovery-clear publication.
13. Visible-but-ambiguous reconciliation bytes never clear recovery.
14. Same-path instances share ambiguity state; exact re-durability clears it.
15. Startup establishes reconciliation-file durability before using rows to
    clear recovery.
16. Newly durable reconciliation is publication-fenced from write-side recovery
    refresh until pending confirmation tokens are invalidated.
17. Confirmation token issue/validate/consume/invalidate is synchronized.
18. Terminal reconciliation invalidates all pending pre-resolution confirmation
    tokens in active supported runtime.
19. Reconciliation does not reset dedupe, token bucket, kill, risk, actor,
    policy, authorization epoch, grant, or permit state.
20. Contradictory terminal reconciliation is corruption; M6 has no correction or
    supersession protocol.
21. Recovery validates both safety ledgers and fails closed if either is corrupt,
    unavailable, or locally durability-ambiguous.
22. Multiple unresolved effects sharing semantic key keep it blocked until all
    are settled/reconciled.
23. Reconciliation clears only recovery uncertainty; never grants execution.
24. Later mutation is fresh M5 invocation and cannot use pre-reconciliation
    human confirmation authority.
25. Failed/missing observation is never generic proof of no effect.
26. Browser evidence inspection respects M5 read/composer coordination.
27. Reconciliation safety facts have no automatic rotation/TTL/deletion in M6.
28. Invocation journal content has zero reconciliation/recovery authority.
29. M6 makes no new cross-process linearizability or exactly-once claim.
30. Replay-safety promotion requires concrete broker-level evidence.
31. Platform qualification claims are bounded to environment tested.
32. M6 does not claim cryptographic integrity against hostile local state-file
    editing.

---

## 20. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| R1 | Raw `EFFECT_UNKNOWN`, no reconciliation | Matching semantic key recovery-blocked |
| R2 | Raw `RESERVED` after restart, no reconciliation | Effective unknown; recovery-blocked |
| R3 | `CONFIRMED_EFFECT` durably reconciles unknown | `RECONCILED_EFFECT`; clear only after known durability/publication ordering |
| R4 | `CONFIRMED_NO_EFFECT` durably reconciles unknown | `RECONCILED_NO_EFFECT`; same ordering |
| R5 | Either verdict reconciles raw `RESERVED` with no live attempt | Corresponding reconciled disposition |
| R6 | Inspection inconclusive | No terminal row; block remains |
| R7 | Unknown `effect_id` | Denied; no row |
| R8 | Any lineage field differs | Denied/fail-closed |
| R9 | Target M5 state already settled | Denied invalid target |
| R10 | Live attempt owns effect | Denied `live_attempt_owned` under canonical lifecycle fence |
| R11 | Evidence missing/empty/non-JSON/hash mismatch | Denied before append |
| R12 | Operator authority missing/mismatched | Denied |
| R13 | Append/fsync fails before known durability | Ambiguity latched; recovery cannot clear |
| R14 | Complete bytes visible after fsync failure | Projector unavailable; exact re-durability required |
| R15 | Exact re-durability succeeds | Latch clears; no duplicate; fact may enter projection |
| R16 | Durable reconciliation then crash before in-memory post-steps | Restart drops pending tokens and recovers reconciliation safely |
| R17 | Same terminal fact exact retry | Re-fsync allowed; no duplicate |
| R18 | Contradictory terminal verdict | Corruption/fail-closed |
| R19 | ReconciliationLedger corrupt | Recovery unavailable |
| R20 | EffectLedger corrupt | M5 fail-closed preserved |
| R21 | Forged journal reconciliation-looking data | No effect on recovery |
| R22 | Two unresolved effects share key; one reconciled | Key remains blocked |
| R23 | All unresolved effects sharing key reconciled | Recovery key clears after durable composite refresh |
| R24 | Reconciliation completes | Old grant/permit/confirmation cannot authorize later mutation |
| R25 | Concurrent reconciliation attempts | At most one terminal fact wins |
| R26 | Evidence collector proposes without operator authority | Cannot append/clear |
| R27 | Browser read fails during inspection | Inconclusive/error, never automatic no-effect |
| R28 | Browser composer owned during inspection | Busy/inconclusive; no competing navigation |
| R29 | Durable resolution but guard refresh fails | Process fail-closed; invalidated confirmations remain invalid |
| R30 | Restart after either verdict | Histories preserved; old authority dead; recovery contribution clear |
| R31 | Second ledger instance after ambiguous append | Shared ambiguity prevents clear |
| R32 | Restart with complete surviving reconciliation row | Startup fsync succeeds before row may clear recovery |
| R33 | Startup reconciliation re-durability fails | Recovery unavailable |
| R34 | Authority retries ambiguous append | Only exact frozen fact accepted |
| R35 | Pending token minted before reconciliation | Token invalidated before clear can become visible to writes |
| R36 | Unrelated pending token exists | Also invalidated conservatively |
| R37 | Write-side guard refresh races durable reconciliation before invalidation | Publication fence forces old-blocked or post-invalidation-clear ordering; never clear-with-old-token-valid |
| R38 | Token validation/consume races global invalidation | Synchronized token store yields one ordering; invalidated token cannot later cross |
| R39 | `CONFIRMED_NO_EFFECT` while dedupe entry live | Recovery clears; independent dedupe may still deny |
| R40 | Token bucket exhausted before reconciliation | No refund; future invocation remains bucket-governed |
| R41 | Kill tripped during reconciliation | Local reconciliation may complete; kill remains tripped |
| R42 | Automatic rotation/TTL attempted | Unsupported/rejected; safety fact retained |
| R43 | Windows durability qualification | Claim matches actual behavior; stronger unsupported claim rejected |

Additional mandatory regressions:

- canonical evidence hash deterministic; mismatch rejected;
- strict JSON + non-empty identity validation;
- reserved evidence keys cannot forge runtime correlation fields;
- same-path reconciliation writers serialize process-locally;
- projection publication cannot regress newer blocked/unavailable truth to older
  clear truth;
- one effect reconciliation cannot clear another unresolved same-key effect;
- no Dispatcher capability can obtain ReconciliationAuthority;
- read-only inspection performs no external mutation;
- operator identity distinct from X actor identity;
- positive evidence cannot infer causal identity from content equality unless
  that predicate is qualified;
- negative evidence cannot infer no-effect from failed observation;
- missing reconciliation file is valid empty history; corruption is not;
- reconciliation row with missing EffectLedger target is corruption;
- audit-journal absence/corruption is irrelevant;
- M6 does not alter M5 monotonic grant/permit clocks;
- M6 does not weaken M5 kill/epoch/policy/permit validation.

---

## 21. Build order

```text
1. Reconciliation record model + fsync-backed ReconciliationLedger
2. ReconciliationPublicationFence + composite RecoveryProjector/RecoveryGuard
3. Synchronized confirmation-token invalidation boundary
4. ReconciliationCoordinator + local operator authority/workflow
5. Fault/restart/corruption/concurrency qualification
6. Windows durability qualification for both safety ledgers
7. Evidence-driven replay-safety qualification (like/unlike first candidate)
```

Each layer uses:

```text
maintainer-first exhaustive review
→ explicit findings register
→ freeze exact candidate
→ CI
→ independent Codex/GitWire review when available
→ reconcile findings
→ exact-head validation
→ pinned merge
```

Unavailable independent tooling is replaced by a distinct recorded adversarial
second pass, never assumed approval.

---

## 22. M6 completion criteria

M6 is complete only when:

```text
✓ historical M5 uncertainty remains immutable
✓ terminal reconciliation is separately durable and lineage-bound
✓ evidence cannot silently become authority
✓ inconclusive/missing evidence keeps unresolved effects blocked
✓ visible-but-not-known-durable reconciliation never clears recovery
✓ reconciliation publication is linearized with stale-confirmation invalidation
✓ pre-resolution human confirmation cannot cross a newly cleared recovery gate
✓ reconciliation does not reset independent policy controls
✓ old execution authority is never restored
✓ restart recomputes same composite recovery truth
✓ corruption/ambiguity in either safety source fails closed
✓ browser inspection preserves M5 composer/read coordination
✓ reconciliation safety history has no audit-style expiry
✓ operator workflow has no normal capability bypass
✓ Windows durability claims have actual platform evidence
✓ like/unlike is evidence-promoted or deliberately remains conservative
```

M6 does not claim cross-process coordination. Multiple independent
runtime/recovery processes sharing one state directory create the M7 forcing
function for a separate authority/locking design.
