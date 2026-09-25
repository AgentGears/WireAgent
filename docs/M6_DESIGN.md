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
7. crash, corruption, restart, ambiguity, and exact-fact retry semantics;
8. a narrow local operator recovery workflow;
9. stale-confirmation invalidation across a reconciliation boundary;
10. platform qualification of durability claims, especially Windows;
11. evidence-driven replay-safety qualification such as like/unlike without
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
editable in M6. Corrective/superseding reconciliation would need its own explicit
history semantics and is a future design problem.

---

## 3. Constitutional distinctions

M6 preserves these independent concepts:

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
UNKNOWN            != NO_EFFECT
```

Project-wide evidence laws apply directly:

- **Evidence ≠ claim.** An observation supports a bounded statement; it does not
  automatically authorize a reconciliation verdict.
- **Cognition ≠ authority.** An agent/model may collect, summarize, or propose;
  it does not commit terminal recovery truth.
- **Execution ≠ external effect.** A successful call path does not establish
  that the remote effect occurred.
- **Verification ≠ generalization.** A proof predicate is valid only for the
  action/environment/evidence class actually qualified.

---

## 4. Terminology

| Term | Meaning |
|---|---|
| **Unresolved effect** | An EffectLedger effect whose raw latest state is `RESERVED` or `EFFECT_UNKNOWN`. |
| **Reconciliation evidence** | Structured observations presented as the basis for an operator decision. Evidence is not itself authority. |
| **Reconciliation verdict** | One durable operator-authorized terminal statement: `CONFIRMED_EFFECT` or `CONFIRMED_NO_EFFECT`. |
| **ReconciliationLedger** | Fsync-backed append-only safety ledger at `.webwire/reconciliations.ndjson`. |
| **RecoveryProjector** | Validated join of EffectLedger and ReconciliationLedger into current recovery truth. |
| **RecoveryGuard** | Process-local enforcement cache/gate derived from the composite projection. |
| **ReconciliationAuthority** | Narrow local authority bound to one immutable reconciliation fact after explicit operator confirmation. |
| **EvidenceCollector** | Read-only mechanism that gathers bounded observations and may propose a verdict; it cannot commit one. |
| **Fresh invocation** | A new normal capability execution with new human confirmation and new M5 grant/attempt lineage. |
| **Live attempt ownership** | A current in-process M5 attempt still capable of writing a terminal outcome for the target effect. Reconciliation may not race it. |
| **Durability-ambiguous reconciliation** | A complete row may be visible after append reported fsync/directory-sync failure; it is not recovery authority until re-durability succeeds. |
| **Confirmation invalidation boundary** | Successful durable terminal reconciliation invalidates all pending pre-resolution WriteKernel confirmation tokens in the active supported runtime before a clear projection is published. |

---

## 5. Separate reconciliation history

M6 does not add successors to M5 `EffectState`.

M5 remains:

```text
RESERVED -> NO_EFFECT | EFFECT_CONFIRMED | EFFECT_UNKNOWN
EFFECT_UNKNOWN is terminal
```

Reconciliation asks a different question:

```text
What later evidence justifies resolving the operational uncertainty created by
that immutable historical fact?
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

This ensures:

1. M5 history stays readable under its frozen schema/state machine.
2. Reconciliation cannot masquerade as an original execution outcome.
3. Evidence provenance stays distinct from effect provenance.
4. Recovery is recomputed from two append-only histories without rewriting
   either one.

The invocation journal is neither ledger.

---

## 6. Reconciliation verdicts

Exactly two terminal reconciliation verdicts exist:

```text
CONFIRMED_EFFECT
CONFIRMED_NO_EFFECT
```

There is intentionally **no durable `INCONCLUSIVE` resolution state**.

An inconclusive inspection appends no recovery-authoritative fact. Audit may
record the inspection, but RecoveryGuard remains blocking.

### 6.1 `CONFIRMED_EFFECT`

Meaning:

> Later evidence plus explicit operator authority establish, within the stated
> evidence boundary, that the uncertain external effect should be treated as
> having occurred.

Consequences:

- original EffectLedger history remains unchanged;
- this effect no longer contributes an unresolved RecoveryGuard block;
- original attempt is never resumed;
- old approval/permit/confirmation authority is not restored;
- a later identical semantic action must traverse the ordinary policy pipeline
  and obtain new human confirmation;
- M6 creates no permanent cross-restart dedupe ban for confirmed effects.

This matches ordinary M5 `EFFECT_CONFIRMED`: known historical occurrence is not
an indefinite ban on a future newly approved identical intent.

### 6.2 `CONFIRMED_NO_EFFECT`

Meaning:

> Later evidence plus explicit operator authority establish, within the stated
> evidence boundary, that the uncertain attempt did not produce the approved
> external effect.

Consequences are the same authority-wise: history stays immutable, the recovery
uncertainty contribution is removed, old authority remains dead, and any later
mutation is a fresh normal invocation.

### 6.3 Reconciliation clears only the recovery uncertainty gate

A terminal reconciliation does **not** guarantee immediate executability.
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

M6 does not automatically remove a DedupeStore entry or refund TokenBucket
capacity. In particular, a newly reconciled `CONFIRMED_NO_EFFECT` may still be
blocked temporarily by process-local defense-in-depth state. That is not a
recovery inconsistency; the recovery gate is only one gate.

### 6.4 Absence is not automatically `CONFIRMED_NO_EFFECT`

The following alone are insufficient:

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

Negative proof requires an action-specific conclusive predicate or an explicit
operator decision grounded in preserved evidence. M6 initially defines no
generic automatic negative-proof predicate.

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

Every copied lineage field exactly equals the canonical first EffectLedger
record for `effect_id`.

The target EffectLedger history must currently be raw:

```text
RESERVED | EFFECT_UNKNOWN
```

Unknown effects, settled M5 effects, or changed lineage are invalid targets.

### 7.2 Evidence and canonical hash

Evidence is strict portable JSON using the M5 no-coercion posture. Minimum:

```text
basis:          non-empty project-defined string
observed_at:    UTC provenance string
observations:   non-empty list of structured observations
```

`evidence_hash` is computed over canonical JSON:

```text
UTF-8
sorted object keys
compact separators
finite JSON numbers only
no non-JSON coercion
SHA-256 -> lowercase hexadecimal
```

Hash mismatch invalidates the record. `ReconciliationAuthority` binds this hash
so evidence cannot change after approval.

External artifacts carry content identity, not only mutable location:

```text
artifact:
  kind: ...
  sha256: ...
  location: optional diagnostic locator
```

A path/URL alone is not evidence identity.

Evidence is minimized to facts required for the bounded reconciliation claim.
Raw page dumps, cookies, private timelines, or unrelated DOM content are not
stored by default. Sensitive artifacts belong outside the ledger and are
referenced by content digest only when necessary.

### 7.3 Operator identity

`operator_id` identifies local human/operator authority. It is not X `actor_id`
and is not inferred from it. Under M6's local single-user model this is durable
provenance, not RBAC.

---

## 8. ReconciliationLedger contract

For one `effect_id`:

```text
no reconciliation -> one terminal reconciliation verdict
terminal reconciliation -> no successor
```

A second different verdict is corruption, not supersession or “last row wins.”

An exact retry may re-establish durability without a duplicate. Exactness
includes:

```text
reconciliation_id
effect_id
all copied lineage
verdict
operator_id
evidence_hash
evidence
```

Timestamp is provenance and may be ignored only for exact-fact re-durability.

### 8.1 Durability

`ReconciliationLedger` uses the M5 safety posture:

- append-only NDJSON;
- owner-writable state file;
- full write loop;
- file `fsync` before success;
- parent-directory fsync for newly created entries where exposed;
- process-local same-path writer serialization;
- schema/history corruption fails closed;
- exact-fact re-durability after ambiguous append failure.

### 8.2 Durability-ambiguity latch

Trusting visible but not known-durable `CONFIRMED_NO_EFFECT` could remove a
safety block. Visibility cannot stand in for durability.

For every normalized reconciliation-ledger path, the process maintains shared
ambiguity state for a fact whose append may have written bytes but did not report
complete file + required directory durability.

```text
append reports ambiguity
→ exact fact becomes locally AMBIGUOUS
→ composite recovery projection for that path unavailable/fail-closed
→ visible row MUST NOT clear RecoveryGuard
→ exact-fact re-durability re-fsyncs file/directory as applicable
→ only successful re-durability clears AMBIGUOUS
→ projector may then use the fact
```

Every same-path `ReconciliationLedger` instance shares writer locking and
ambiguity state.

After process restart the in-memory latch is gone. Before startup recovery trusts
an existing reconciliation file, the supported reader establishes current file
durability with a writable-handle fsync and parent-directory fsync where
applicable. Failure makes recovery unavailable.

### 8.3 Retention

`reconciliations.ndjson` is safety state, not audit output.

M6 defines:

```text
no TTL expiry
no monthly rotation
no best-effort pruning
no independent deletion
no compaction
```

A reconciliation fact is retained for at least as long as the corresponding
EffectLedger history can participate in recovery. Any future compaction/archival
scheme must preserve the joined semantic history atomically and is outside M6.

A missing reconciliation file is interpreted as empty reconciliation history;
that can conservatively re-block unresolved effects but can never manufacture a
clear state. A reconciliation record whose target effect is missing from the
EffectLedger is corruption/fail-closed.

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

Action-specific evidence policy may prove more only after that predicate is
explicitly designed and regression-qualified.

### 9.1 Positive evidence

Positive remote identity can be strong when it uniquely binds to approved effect
lineage: e.g. exact remote object identity plus actor, target, and content proof
under a qualified action-specific reconciler.

If the observation cannot distinguish this attempt from a pre-existing or
externally created equivalent object, the collector reports ambiguity. It may
not upgrade correlation into causal identity.

### 9.2 Negative evidence

Negative evidence has a higher claim burden. Read failure is not absence;
observed absence is not automatically proof that no effect ever occurred.

`CONFIRMED_NO_EFFECT` requires either:

1. an action-specific conclusive no-effect predicate explicitly accepted and
   regression-qualified; or
2. explicit operator resolution with basis and limitations preserved in durable
   evidence.

### 9.3 Evidence collector boundary

An `EvidenceCollector` may:

- read canonical effect lineage;
- perform bounded read-only remote inspection;
- produce structured observations;
- state `suggested_verdict` or `inconclusive` as non-authoritative analysis.

It may not:

- append a ReconciliationRecord;
- clear RecoveryGuard;
- construct execution authority;
- mutate external state as part of inspection;
- infer no-effect from read failure.

Browser-backed inspection uses the same M5 leased-read/browser-state
coordination as ordinary evidence reads. If a content composer/owner holds the
browser state, inspection returns busy/inconclusive and does not navigate across
the owner.

---

## 10. Reconciliation authority

Clearing a recovery block is a safety-relevant authority transition. It is not a
normal capability write and does not reuse WriteKernel confirmation authority.

`ReconciliationAuthority` is:

- local/operator-facing only;
- absent from normal agent capability registration;
- without raw browser mutation surface;
- created only after explicit human/operator confirmation of effect, verdict,
  canonical evidence hash, and evidence summary in the same local session;
- bound exactly to:

```text
effect_id
verdict
evidence_hash
operator_id
```

- unable to mint ApprovalGrant/EffectPermit or mutate EffectLedger;
- able only to request persistence of that bound reconciliation fact.

Programmatic non-interactive model-authorized terminal reconciliation is out of
scope.

### 10.1 One authority, one fact, possibly multiple durability attempts

“Single-use” means one immutable reconciliation **fact**, not one filesystem
syscall.

Before first durability I/O the coordinator freezes the complete record and
verifies its evidence hash against approved authority.

If persistence reports ambiguity, the same authority lineage may re-drive
**only the exact frozen fact** until durability is established or the operation
is abandoned. Verdict, evidence, lineage, and operator identity cannot change.

After known durable success the authority is consumed permanently.

### 10.2 Recovery-only, not in-flight rescue

Reconciliation may not race a live in-process M5 attempt capable of writing a
terminal EffectLedger outcome for the same `effect_id`.

```text
live nonterminal attempt owns effect_id
    -> denied: live_attempt_owned
```

After restart ephemeral attempts/grants are gone; stale durable `RESERVED` is
eligible under the normal single-process ownership assumption.

### 10.3 Kill-switch relationship

The kill switch blocks external capability execution; it does not block local
read-only inspection or durable reconciliation bookkeeping. Operators must be
able to reconcile while mutation is deliberately killed.

Reconciliation:

- does not reset kill state;
- does not decrement authorization epoch;
- does not mint execution authority;
- leaves every future mutation subject to the still-current kill/epoch gates.

---

## 11. Reconciliation protocol

Conceptually:

```text
resolve(effect_id, verdict, evidence, operator_authority)
```

Required ordering:

```text
acquire reconciliation protocol fence
→ verify no live nonterminal M5 attempt owns effect_id
→ fully read/validate EffectLedger
→ require target effect raw RESERVED | EFFECT_UNKNOWN
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
→ invalidate all pending pre-resolution WriteKernel confirmation tokens in the
  active supported runtime
→ refresh composite RecoveryProjector/RecoveryGuard
→ return durable reconciliation result
```

No recovery-clear publication occurs before durable success **and** pending
pre-resolution confirmations are invalidated.

If append reports ambiguity, the coordinator retains the frozen fact/bounded
authority lineage only for exact-fact re-durability. Recovery remains
fail-closed.

### 11.1 Why pending confirmations are invalidated

Current WriteKernel confirmation tokens are in-memory, capability/intent-bound,
single-use, and time-limited. A token can nevertheless remain unconsumed while
RecoveryGuard denies its second phase. If reconciliation later clears the guard,
that old token would otherwise cross without a new post-reconciliation human
confirmation.

M6 therefore makes durable reconciliation a **global pending-confirmation
invalidation boundary for the active supported runtime**. Invalidating unrelated
pending tokens is intentionally conservative; reconciliation is rare and
safety-sensitive.

After process restart the token store is already empty.

### 11.2 Concurrency

Within the supported process:

- reconciliation operations serialize under one protocol fence;
- competing terminal verdicts for one effect yield at most one durable winner;
- terminal M5 outcome writing and reconciliation do not race the same live effect
  because live-attempt ownership is a hard precondition;
- composite projection reads validated ledger histories in one documented order
  and publishes only complete joined snapshots;
- an older clear snapshot never overwrites newer blocked/unavailable truth;
- same-path reconciliation instances share writer lock + ambiguity state.

Independent external processes remain uncoordinated/out of scope until M7.

---

## 12. Composite recovery projection

M6 enforcement joins both safety histories:

```text
EffectLedger -----------\
                         -> RecoveryProjector -> RecoveryGuard
ReconciliationLedger ---/
```

`EffectLedger.recovery_projection()` may remain an M5 diagnostic/helper; M6
enforcement uses the composite projector.

| Raw M5 state | Reconciliation | Composite disposition | Guard |
|---|---|---|---|
| `NO_EFFECT` | none | `SETTLED_NO_EFFECT` | clear |
| `EFFECT_CONFIRMED` | none | `SETTLED_EFFECT` | clear |
| `RESERVED` | none | `UNRESOLVED_UNKNOWN` | block |
| `EFFECT_UNKNOWN` | none | `UNRESOLVED_UNKNOWN` | block |
| `RESERVED` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | recovery-clear |
| `EFFECT_UNKNOWN` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | recovery-clear |
| `RESERVED` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | recovery-clear |
| `EFFECT_UNKNOWN` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | recovery-clear |

Impossible combinations—including reconciliation attached to an unknown or
already-settled M5 effect—are corruption and make recovery unavailable.

A durability-ambiguous reconciliation is not projection authority even when its
bytes are visible.

### 12.1 Recovery-clear is not old-attempt replay

After either verdict:

```text
old confirmation token: invalidated/not reconstructed
old ApprovalGrant:       not reconstructed; never reopened
old EffectPermit:        not reconstructed/reused
old EffectAttempt:       historical only
```

A later mutation must eventually traverse:

```text
current independent policy gates
→ new preview/human confirmation
→ new ApprovalGrant
→ new EffectAttempt/effect_id
→ current RecoveryGuard check as ordered by runtime
→ normal M5 authority protocol
```

The implementation may preserve the existing WriteKernel gate order; the
architectural requirement is that no pre-resolution confirmation authority can
cross after reconciliation and all ordinary gates still apply.

---

## 13. RecoveryGuard semantics after M6

RecoveryGuard remains process-local enforcement derived from durable truth.

Required behavior:

1. startup hydration validates both safety ledgers before browser mutation is
   available;
2. existing reconciliation history is re-durability-fsynced before it may clear
   recovery in a new process;
3. every supported M5 mutation refreshes the composite projection before the
   browser-capable preview, preserving M5's same-process stale-cache fix;
4. if either ledger cannot be read/validated/re-durability-established, cached
   clear state is discarded and mutation fails closed;
5. only `UNRESOLVED_UNKNOWN` contributes a recovery semantic-key block;
6. terminal reconciliation removes only that recovery contribution after known
   durability + confirmation invalidation;
7. diagnostic health/status never substitutes for enforcement;
8. invocation-journal content cannot create, clear, or modify recovery truth.

If multiple unresolved effects share a semantic key, reconciling one does not
clear the key while another remains unresolved.

---

## 14. Failure and crash semantics

### 14.1 Crash before append

No durable reconciliation exists; guard remains blocking.

### 14.2 Complete bytes visible but fsync reports failure

Fact is locally durability-ambiguous. Composite projection is unavailable for
that path. Exact-fact retry re-establishes durability without duplicate row.

### 14.3 Durable append, crash before confirmation invalidation / guard refresh

After restart pending confirmation tokens are gone by process death. Startup
re-durability and composite projection recover the reconciliation fact. Safety
does not depend on an in-memory post-append step.

### 14.4 Durable append, confirmation invalidation succeeds, guard refresh fails

Current process remains fail-closed until refresh succeeds. Invalidated tokens
stay invalid; they are not restored because refresh failed.

### 14.5 Corrupt ReconciliationLedger

RecoveryGuard is unavailable. Do not fall back to EffectLedger-only projection.

### 14.6 Corrupt EffectLedger

Existing M5 fail-closed rule is unchanged.

### 14.7 Invocation journal failure/corruption

No effect on M6 authority.

---

## 15. Operator workflow

M6 supports a narrow local recovery workflow, not a general CLI platform.

Conceptual commands:

```text
webwire recovery list
webwire recovery show <effect_id>
webwire recovery inspect <effect_id>
webwire recovery resolve <effect_id> --verdict confirmed-effect
webwire recovery resolve <effect_id> --verdict confirmed-no-effect
```

Exact syntax is implementation detail. Frozen authority semantics:

- `list/show/inspect` are read-only;
- browser-backed `inspect` uses leased-read coordination and returns busy rather
  than navigating across an owned composer;
- inspection may remain inconclusive;
- `resolve` presents effect lineage, verdict, canonical evidence hash, and
  evidence summary to the human operator;
- terminal resolution requires explicit interactive confirmation in the same
  local session;
- no hidden non-interactive Dispatcher capability bypass exists;
- `resolve` performs no compensating remote write;
- resolution is allowed while the kill switch is tripped but does not reset it;
- after resolution any mutation is a separate normal invocation.

The supported workflow assumes exclusive WireAgent process ownership. Concurrent
independent recovery/runtime processes are unsupported.

---

## 16. Windows durability qualification

Current durability CI evidence is Ubuntu. M6 must qualify target-platform claims
rather than restate them.

Windows qualification covers both safety ledgers:

- state-directory/file creation behavior;
- append and writable-handle flush behavior;
- exact-fact re-durability after simulated ambiguous flush failure;
- reconciliation ambiguity-latch behavior;
- startup reconciliation-file re-durability before projection;
- truncated/corrupt tail fail-closed behavior;
- restart projection from durable files;
- parent-directory behavior according to what Python/Windows actually exposes;
- no stronger persistence claim than evidence supports.

Falsified assumptions revise design/runtime. Successful evidence is bounded
platform qualification, not universal filesystem proof.

---

## 17. Replay-safety qualification

M6 may investigate conservative M5 replay classifications, independently from
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

A higher-level pre-state check is insufficient. Promotion to `SAFE_STATE_SET` /
`BEST_EFFORT` requires concrete broker implementation + regression evidence.

Failed qualification is retained evidence; conservative policy stays unchanged.

---

## 18. Frozen M6 invariants

1. M5 EffectLedger history is never rewritten by reconciliation.
2. `EFFECT_UNKNOWN` remains terminal historical M5 state.
3. Reconciliation is a separate durable evidence/authority axis.
4. Only raw `RESERVED` or `EFFECT_UNKNOWN` effects are valid targets.
5. Reconciliation lineage exactly matches canonical M5 effect lineage.
6. Exactly two terminal verdicts exist: `CONFIRMED_EFFECT` and
   `CONFIRMED_NO_EFFECT`.
7. Inconclusive evidence appends no recovery-authoritative terminal fact.
8. Every terminal fact carries strict evidence, canonical evidence hash, and
   explicit operator identity.
9. Evidence collectors may propose; they may not commit reconciliation truth.
10. ReconciliationAuthority binds one immutable effect/verdict/evidence-hash fact
    and may re-drive only that fact through durability ambiguity.
11. A live in-process attempt cannot be reconciled while it can still emit an M5
    terminal outcome.
12. Known reconciliation durability precedes recovery-clear publication.
13. Visible-but-ambiguous reconciliation bytes never clear recovery.
14. Same-path ledger instances share local ambiguity state; exact re-durability
    is the only in-process way to clear it.
15. Startup establishes current reconciliation-file durability before using
    existing rows to clear recovery.
16. Terminal reconciliation invalidates every pending pre-resolution WriteKernel
    confirmation token before clear publication.
17. Reconciliation does not reset dedupe, token-bucket, kill, risk, actor,
    policy, authorization-epoch, grant, or permit state.
18. Contradictory terminal reconciliation is corruption; M6 has no correction or
    supersession protocol.
19. Recovery validates both safety ledgers and fails closed if either source is
    corrupt, unavailable, or locally durability-ambiguous.
20. Multiple unresolved effects sharing a semantic key keep it blocked until all
    are settled/reconciled.
21. Reconciliation clears only recovery uncertainty; it never grants execution
    permission.
22. A later mutation is a fresh normal M5 invocation and cannot use
    pre-reconciliation human confirmation authority.
23. Failed/missing observation is never generic proof of no effect.
24. Browser evidence inspection respects M5 read/composer coordination.
25. Reconciliation safety facts have no automatic rotation/TTL/deletion in M6.
26. Invocation journal content has zero reconciliation/recovery authority.
27. M6 makes no new cross-process linearizability or exactly-once claim.
28. Replay-safety promotion requires concrete broker-level evidence.
29. Platform qualification claims remain bounded to the environment tested.
30. M6 does not claim cryptographic integrity against a hostile local state-file
    editor.

---

## 19. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| R1 | Raw `EFFECT_UNKNOWN`, no reconciliation | Matching semantic key recovery-blocked |
| R2 | Raw `RESERVED` after restart, no reconciliation | Effective unknown; recovery-blocked |
| R3 | `CONFIRMED_EFFECT` durably reconciles `EFFECT_UNKNOWN` | `RECONCILED_EFFECT`; recovery contribution removed only after known durability |
| R4 | `CONFIRMED_NO_EFFECT` durably reconciles `EFFECT_UNKNOWN` | `RECONCILED_NO_EFFECT`; recovery contribution removed only after known durability |
| R5 | Either verdict reconciles raw `RESERVED` with no live attempt | Corresponding reconciled disposition |
| R6 | Inspection is inconclusive | No terminal row; block remains |
| R7 | Unknown `effect_id` | Denied; no row |
| R8 | Any copied lineage field differs | Denied/fail-closed; no row |
| R9 | Target M5 state already `NO_EFFECT`/`EFFECT_CONFIRMED` | Denied invalid target |
| R10 | Live nonterminal attempt owns effect | Denied `live_attempt_owned` |
| R11 | Evidence missing/empty/non-JSON/hash mismatch | Denied before append |
| R12 | Operator authority missing/mismatched | Denied |
| R13 | Append/fsync fails before known durability | Ambiguity latched; recovery cannot clear |
| R14 | Complete bytes visible after fsync failure | Projector unavailable; exact re-durability required |
| R15 | Exact re-durability succeeds | Latch clears; no duplicate; fact may enter projection |
| R16 | Durable reconciliation then crash before in-memory post-steps | Restart loses pending tokens and derives reconciliation after startup durability establishment |
| R17 | Same terminal fact exact retry | Re-fsync allowed; no duplicate |
| R18 | Contradictory terminal verdict | Corruption/fail-closed |
| R19 | ReconciliationLedger malformed/torn/corrupt | Recovery unavailable; mutation denied |
| R20 | EffectLedger malformed/torn/corrupt | Existing M5 fail-closed preserved |
| R21 | Forged invocation-journal reconciliation-looking data | No effect on projection/guard |
| R22 | Two unresolved effects share key; only one reconciled | Key remains recovery-blocked |
| R23 | All unresolved effects sharing key reconciled | Recovery key clears after durable composite refresh |
| R24 | Reconciliation completes | Old grant/permit/confirmation cannot authorize later mutation |
| R25 | Concurrent reconciliation attempts | At most one terminal fact wins |
| R26 | Evidence collector proposes verdict without operator authority | Cannot append/clear |
| R27 | Browser read fails during inspection | Inconclusive/error; never automatic no-effect |
| R28 | Browser composer is owned during inspection | Inspection returns busy/inconclusive; no competing navigation |
| R29 | Durable resolution exists but guard refresh fails | Process stays fail-closed; invalidated confirmations remain invalid |
| R30 | Restart after either terminal verdict | Unknown M5 history + reconciliation preserved; old authority dead; recovery contribution cleared |
| R31 | Second ledger instance opens same path after ambiguous append | Shared ambiguity state prevents clear |
| R32 | Restart with complete surviving reconciliation row | Startup fsync succeeds before row may clear recovery |
| R33 | Startup reconciliation re-durability fails | Recovery unavailable/fail-closed |
| R34 | Authority retries ambiguous append | Only exact frozen fact accepted; changes rejected |
| R35 | Pending confirmation token was minted before reconciliation | Durable reconciliation invalidates token before recovery clear; token cannot execute afterward |
| R36 | Unrelated pending confirmation token exists | It is also invalidated conservatively |
| R37 | `CONFIRMED_NO_EFFECT` while semantic dedupe entry still live | Recovery clears but independent dedupe may still deny; no dedupe mutation by reconciliation |
| R38 | Token bucket exhausted before reconciliation | Reconciliation does not refund budget; future normal invocation remains subject to bucket |
| R39 | Kill switch tripped during reconciliation | Local reconciliation may complete; kill remains tripped and mutation remains denied |
| R40 | Automatic rotation/TTL attempted for ReconciliationLedger | Unsupported/rejected; safety fact retained |
| R41 | Windows durability qualification | Claim matches actual file/directory behavior; unsupported stronger claim rejected |

Additional mandatory regressions:

- canonical evidence hash deterministic; mismatch rejected;
- strict JSON validation and genuine non-empty identity strings;
- reserved evidence keys cannot forge runtime correlation fields;
- same-path reconciliation writers serialize process-locally;
- projection publication cannot regress from newer blocked/unavailable truth to
  older clear truth;
- reconciliation for one effect cannot clear another unresolved effect sharing
  the semantic key;
- no Dispatcher capability can obtain `ReconciliationAuthority`;
- read-only inspection performs no external mutation;
- operator identity is distinct from X actor identity;
- positive evidence never infers causal identity from content equality unless
  that exact predicate is qualified;
- negative evidence never infers no-effect from failed observation;
- missing reconciliation file is valid empty history; corruption is not;
- a reconciliation row with missing EffectLedger target is corruption;
- audit-journal absence/corruption remains irrelevant;
- M6 does not alter M5 monotonic grant/permit clocks;
- M6 does not weaken M5 kill/epoch/policy/permit validation.

---

## 20. Build order

```text
1. Reconciliation record model + fsync-backed ReconciliationLedger
2. Composite RecoveryProjector + RecoveryGuard integration
3. ReconciliationCoordinator + local operator authority/workflow
4. Fault/restart/corruption qualification of complete reconciliation path
5. Windows durability qualification for effect + reconciliation ledgers
6. Evidence-driven replay-safety qualification (like/unlike first candidate)
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
second pass, never by an assumption of approval.

---

## 21. M6 completion criteria

M6 is complete only when:

```text
✓ historical M5 uncertainty remains immutable
✓ terminal reconciliation is separately durable and lineage-bound
✓ evidence cannot silently become authority
✓ inconclusive/missing evidence keeps unresolved effects blocked
✓ visible-but-not-known-durable reconciliation never clears recovery
✓ pending pre-resolution confirmations cannot cross a newly cleared recovery gate
✓ reconciliation does not silently reset independent policy controls
✓ old execution authority is never restored
✓ restart recomputes the same composite recovery truth
✓ corruption/ambiguity in either safety source fails closed
✓ browser evidence inspection preserves M5 composer/read coordination
✓ reconciliation safety history is retained without audit-style expiry
✓ operator workflow exists without a normal capability bypass
✓ Windows durability claims have actual platform evidence
✓ like/unlike is evidence-promoted or deliberately remains conservative
```

M6 does not claim cross-process coordination. Multiple independent
runtime/recovery processes sharing one state directory create the M7 forcing
function for a separate authority/locking design.
