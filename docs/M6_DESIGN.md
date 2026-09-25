# M6 — Evidence-Bearing Reconciliation & Qualification Boundary

```text
Status:   CANDIDATE — MAINTAINER-FIRST REVIEW IN PROGRESS
Base:     main 666064b3c5dc3f905be11321a3604460410dd583
Runtime:  M5 baseline 0c62402ae01b50d7662b3978cbf2bee4109aa035
Scope:    reconcile durable M5 uncertainty without rewriting history;
          qualify the durability/replay claims that remain intentionally bounded
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

M6 adds a second, orthogonal durable axis:

```text
historical effect fact  +  evidence-bearing reconciliation fact
        EffectLedger              ReconciliationLedger
                \                    /
                 \                  /
                  -> RecoveryProjector -> RecoveryGuard
```

The governing rule is:

> **Historical uncertainty is immutable. Reconciliation adds evidence; it does
> not rewrite the original effect fact. Reconciliation may restore eligibility
> for a new invocation, but it never restores old execution authority.**

---

## 1. Problem statement

M5 deliberately stops automatic replay when a REQUIRED effect is durably
uncertain. That is the correct fail-closed behavior, but durable uncertainty
cannot remain operationally permanent.

Two M5 states require later human/operator resolution:

```text
RESERVED         # after restart, mutation may or may not have crossed
EFFECT_UNKNOWN   # runtime explicitly could not establish terminal effect truth
```

Today `RecoveryGuard` can only answer:

```text
unresolved -> block
otherwise  -> clear
```

It has no evidence-bearing way to say that a previously uncertain effect was
later established to have occurred or established not to have occurred.

M6 introduces that missing recovery boundary while preserving the M5 laws that
made uncertainty safe in the first place.

### 1.1 The dangerous shortcuts M6 must reject

M6 must not implement any of these shortcuts:

- mutate `EFFECT_UNKNOWN` into `EFFECT_CONFIRMED` or `NO_EFFECT`;
- interpret a failed browser read, missing selector, timeout, 404, or empty
  result as proof of no external effect;
- let an AI/model/evidence collector clear a recovery block merely because it
  produced a plausible explanation;
- persist or revive an old ApprovalGrant after reconciliation;
- treat a best-effort invocation-journal row as reconciliation evidence or
  recovery authority;
- silently choose the latest of contradictory reconciliation facts;
- infer that matching content/state proves causal identity when the evidence
  cannot distinguish this attempt from another external action;
- solve cross-process coordination incidentally inside reconciliation.

---

## 2. Scope and non-goals

### 2.1 In scope

M6 defines and qualifies:

1. a durable `ReconciliationLedger` separate from `EffectLedger`;
2. exact lineage binding from reconciliation facts to one M5 `effect_id`;
3. evidence and claim semantics for terminal reconciliation;
4. explicit human/operator reconciliation authority;
5. a composite `RecoveryProjector` that joins effect and reconciliation truth;
6. `RecoveryGuard` behavior after terminal reconciliation;
7. crash, corruption, restart, and exact-fact retry semantics for reconciliation;
8. a narrow operator recovery workflow;
9. platform qualification of durability claims, especially Windows;
10. evidence-driven replay-safety promotion work such as like/unlike, without
    presuming a policy upgrade.

### 2.2 Out of scope

M6 does **not** provide:

- distributed or cross-process exactly-once execution;
- cross-process EffectLedger/ReconciliationLedger writer coordination;
- a remote reconciliation service;
- automatic model-authorized resolution;
- automatic terminal reconciliation from browser heuristics;
- persisted ApprovalGrant or EffectPermit authority;
- a generic workflow engine;
- a general-purpose CLI framework;
- multi-user / role-based authorization;
- durable cross-restart rate limits or semantic dedupe for confirmed effects;
- a policy upgrade for like/unlike unless concrete broker evidence earns it;
- rewriting or deletion of historical M5 effect facts.

Cross-process coordination is a separate M7 design problem.

---

## 3. Constitutional distinctions

M6 preserves these independent concepts:

```text
observation        != evidence claim
claim              != reconciliation authority
effect history     != reconciliation history
reconciliation     != old-attempt continuation
eligibility        != execution permission
semantic equality  != causal identity
storage identity   != semantic identity
absence            != proven no-effect
UNKNOWN            != NO_EFFECT
```

The project-wide evidence laws apply directly:

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
| **ReconciliationLedger** | Fsync-backed append-only ledger of terminal reconciliation facts at `.webwire/reconciliations.ndjson`. |
| **RecoveryProjector** | Pure/validated join of EffectLedger and ReconciliationLedger into current recovery truth. |
| **RecoveryGuard** | Process-local enforcement cache/gate derived from the composite recovery projection. |
| **ReconciliationAuthority** | Narrow local authority that may durably commit one terminal reconciliation verdict after explicit operator confirmation. |
| **EvidenceCollector** | Read-only mechanism that gathers bounded observations and may propose a verdict; it cannot commit one. |
| **Fresh invocation** | A new normal capability execution with a new human confirmation and new M5 grant/attempt lineage. |
| **Live attempt ownership** | A current in-process M5 attempt still capable of writing a terminal outcome for the target effect. Reconciliation may not race it. |

---

## 5. Why reconciliation is a separate ledger

M6 does not add successors to the M5 `EffectState` machine.

M5 remains:

```text
RESERVED -> NO_EFFECT | EFFECT_CONFIRMED | EFFECT_UNKNOWN
EFFECT_UNKNOWN is terminal
```

Reconciliation is a different question:

```text
What later evidence justifies resolving the operational uncertainty created by
that immutable historical fact?
```

Therefore the durable model is:

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

This separation has four required properties:

1. M5 history remains readable under its frozen schema and state machine.
2. A reconciliation bug cannot masquerade as an original execution outcome.
3. Evidence provenance remains distinguishable from effect provenance.
4. Recovery can be recomputed from two append-only histories without rewriting
   either one.

The invocation journal is not either ledger and remains irrelevant to recovery
truth.

---

## 6. Reconciliation verdicts

M6 defines exactly two terminal reconciliation verdicts:

```text
CONFIRMED_EFFECT
CONFIRMED_NO_EFFECT
```

There is intentionally **no durable `INCONCLUSIVE` resolution state**.

An inconclusive inspection means no terminal reconciliation fact is appended;
the effect remains unresolved and RecoveryGuard remains blocking. An audit
record may state that an inspection was inconclusive, but audit does not alter
recovery authority.

### 6.1 `CONFIRMED_EFFECT`

Meaning:

> Later evidence plus explicit operator authority establish, within the stated
> evidence boundary, that the uncertain external effect should be treated as
> having occurred.

Consequences:

- the old EffectLedger fact remains unchanged;
- the recovery-uncertainty block is cleared for future **fresh invocations**;
- the original attempt is never resumed;
- no old ApprovalGrant, EffectPermit, confirmation token, or claim is restored;
- a later identical semantic action still requires the normal fresh human
  confirmation path;
- M6 does not create a permanent cross-restart dedupe ban for confirmed effects.

This matches ordinary M5 `EFFECT_CONFIRMED`: known historical occurrence is not
an indefinitely persisted prohibition on a future, newly approved identical
intent.

### 6.2 `CONFIRMED_NO_EFFECT`

Meaning:

> Later evidence plus explicit operator authority establish, within the stated
> evidence boundary, that the uncertain attempt did not produce the approved
> external effect.

Consequences:

- the old EffectLedger fact remains unchanged;
- the recovery-uncertainty block is cleared;
- the original grant/attempt remains historical and is not reusable;
- any retry is a fresh invocation with fresh human confirmation and new M5
  authority lineage.

### 6.3 Absence is not automatically `CONFIRMED_NO_EFFECT`

The following are **not**, by themselves, sufficient proof of no effect:

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
operator decision grounded in recorded evidence. M6 initially defines no generic
automatic negative-proof predicate.

---

## 7. Reconciliation record schema

The logical record is:

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
  evidence:           non-empty strict-JSON object
  timestamp:          UTC wall-clock provenance string
```

### 7.1 Lineage equality

For a reconciliation to be valid, every copied lineage field must exactly equal
the canonical first EffectLedger record for the same `effect_id`.

The target EffectLedger history must also currently project unresolved from raw:

```text
RESERVED | EFFECT_UNKNOWN
```

A reconciliation record for an unknown effect, a terminal M5 effect, or changed
lineage is invalid.

### 7.2 Evidence requirements

`evidence` is strict portable JSON under the same no-coercion rule as M5 durable
evidence. At minimum it must contain:

```text
basis:          non-empty project-defined string
observed_at:    UTC provenance string
observations:   non-empty list of structured observations
```

Optional external artifacts must carry content identity, not only a mutable path:

```text
artifact:
  kind: ...
  sha256: ...
  location: optional diagnostic locator
```

A path/URL alone is not durable evidence identity.

Reserved correlation fields owned by the reconciliation runtime cannot be
silently overwritten by caller-provided evidence.

### 7.3 Operator identity

`operator_id` identifies the local human/operator authority that committed the
resolution. It is not the X actor identity and must not be inferred from
`actor_id`.

M6 remains single-user/local, so this is provenance rather than a multi-user RBAC
system.

---

## 8. ReconciliationLedger history contract

For one `effect_id`:

```text
no reconciliation
    -> one terminal reconciliation verdict

terminal reconciliation
    -> no successor
```

There is at most one authoritative terminal reconciliation for an effect.

A second different verdict is corruption, not supersession and not “last row
wins.”

An exact retry of the **same reconciliation fact** may re-establish durability
without appending a duplicate. Exactness includes:

```text
reconciliation_id
effect_id
all copied lineage
verdict
operator_id
evidence
```

Timestamp is provenance and may be ignored only for same-fact re-durability,
matching the M5 exact-fact retry principle.

### 8.1 Durability

`ReconciliationLedger` uses the same safety posture as EffectLedger:

- append-only NDJSON;
- owner-writable state file;
- full write loop;
- file `fsync` before success;
- parent-directory fsync for newly created entries where the platform exposes
  that primitive;
- process-local same-path writer serialization;
- read/schema/history corruption fails closed;
- visible bytes plus fsync failure remain ambiguous until exact-fact retry
  re-establishes durability.

A terminal reconciliation does not affect RecoveryGuard until its durable append
has succeeded.

---

## 9. Evidence semantics and claim ceiling

Reconciliation evidence must support the specific claim being made about the
specific effect. M6 does not treat a plausible state match as causal proof.

Examples:

```text
matching post text exists
    != this exact unknown attempt created it

object currently absent
    != this exact attempt produced no effect

current bookmark state is set
    != proof of which actor/process set it
```

An action-specific evidence policy may prove more, but only after that predicate
is explicitly designed and regression-qualified.

### 9.1 Positive evidence

Positive remote identity can be strong evidence when it uniquely binds to the
approved effect lineage, for example an exact remote object identity plus actor,
target, and content proof generated by a qualified action-specific reconciler.

If the observation cannot distinguish the unknown attempt from a pre-existing or
externally created equivalent object, the collector must report the ambiguity.
It may not upgrade correlation into causal identity.

### 9.2 Negative evidence

Negative evidence has a higher claim burden. A read failure is not evidence of
absence, and observed absence is not automatically proof that no effect ever
occurred.

`CONFIRMED_NO_EFFECT` therefore requires either:

1. an action-specific conclusive no-effect predicate that has been explicitly
   accepted and regression-qualified; or
2. explicit operator resolution with the basis and limitations preserved in the
   durable evidence object.

### 9.3 Evidence collector boundary

An `EvidenceCollector` may:

- read the canonical effect lineage;
- perform bounded read-only browser/remote inspection;
- produce structured observations;
- state `suggested_verdict` or `inconclusive` as non-authoritative analysis.

It may not:

- append a ReconciliationRecord;
- clear RecoveryGuard;
- construct execution authority;
- mutate external state as part of inspection;
- infer no-effect from read failure.

---

## 10. Reconciliation authority

Clearing a recovery block is a safety-relevant authority transition. It is not a
normal capability write and does not reuse WriteKernel confirmation authority.

M6 introduces a narrow `ReconciliationAuthority` with these properties:

- local/operator-facing only;
- not registered as a normal agent capability;
- no raw browser mutation surface;
- requires explicit human/operator confirmation of effect, proposed verdict, and
  evidence summary in the same reconciliation session;
- single-use for one `effect_id` + verdict + evidence digest;
- cannot mint ApprovalGrant or EffectPermit;
- cannot mutate EffectLedger history;
- can only request the ReconciliationCoordinator to append one durable terminal
  reconciliation fact.

Programmatic non-interactive model-authorized reconciliation is out of scope for
M6.

### 10.1 Reconciliation is recovery-only, not in-flight rescue

M6 may not reconcile an effect while a live in-process M5 attempt still owns that
`effect_id` and could append its own terminal EffectLedger outcome.

Supported rule:

```text
live nonterminal attempt owns effect_id
    -> reconciliation denied: live_attempt_owned
```

After restart, ephemeral attempts/grants are gone; stale durable `RESERVED`
therefore becomes eligible for operator reconciliation under the normal
single-process ownership assumption.

This prevents reconciliation from racing an active commit/outcome protocol.

---

## 11. Reconciliation protocol

The authoritative operation is conceptually:

```text
resolve(effect_id, verdict, evidence, operator_authority)
```

Required sequence:

```text
acquire reconciliation protocol fence
→ verify no live nonterminal M5 attempt owns effect_id
→ read and fully validate EffectLedger
→ require target effect exists and is raw RESERVED | EFFECT_UNKNOWN
→ capture exact immutable effect lineage
→ read and fully validate ReconciliationLedger
→ require no terminal reconciliation already exists
→ validate operator authority is bound to effect/verdict/evidence digest
→ validate strict evidence + copied lineage
→ append exact ReconciliationRecord
→ fsync ReconciliationLedger
→ only after durable success refresh composite RecoveryProjector/RecoveryGuard
→ return durable reconciliation result
```

No clear/release operation occurs before the fsync-backed reconciliation fact.

### 11.1 Concurrency

Within the supported single process:

- reconciliation operations are serialized by one reconciliation protocol lock;
- competing terminal verdicts for one effect result in at most one durable
  winner;
- terminal M5 outcome writing and reconciliation must not race for the same live
  effect because live-attempt ownership is a hard precondition;
- composite projection must read validated ledger histories in one documented
  order and publish only a complete joined snapshot;
- an older clear snapshot may never overwrite a newer blocked snapshot.

Independent external processes are not coordinated by these locks and remain out
of scope until M7.

---

## 12. Composite recovery projection

M6 moves projection responsibility out of EffectLedger-only recovery semantics
and into a join component:

```text
EffectLedger -----------\
                         -> RecoveryProjector -> RecoveryGuard
ReconciliationLedger ---/
```

`EffectLedger.recovery_projection()` may remain as a raw M5 diagnostic/helper,
but M6 enforcement must use the composite projector.

For each effect:

| Raw M5 state | Reconciliation | Composite recovery disposition | Guard behavior |
|---|---|---|---|
| `NO_EFFECT` | none | `SETTLED_NO_EFFECT` | clear |
| `EFFECT_CONFIRMED` | none | `SETTLED_EFFECT` | clear |
| `RESERVED` | none | `UNRESOLVED_UNKNOWN` | block |
| `EFFECT_UNKNOWN` | none | `UNRESOLVED_UNKNOWN` | block |
| `RESERVED` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | clear for fresh invocation |
| `EFFECT_UNKNOWN` | `CONFIRMED_EFFECT` | `RECONCILED_EFFECT` | clear for fresh invocation |
| `RESERVED` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | clear for fresh invocation |
| `EFFECT_UNKNOWN` | `CONFIRMED_NO_EFFECT` | `RECONCILED_NO_EFFECT` | clear for fresh invocation |

Any impossible combination, including reconciliation attached to an unknown or
already-settled M5 effect, is corruption and makes recovery authority unavailable.

### 12.1 Clearing recovery is not replaying the old attempt

After either terminal reconciliation verdict:

```text
old confirmation token: invalid/expired/not reconstructed
old ApprovalGrant:       not reconstructed; if still present it is not reopened
old EffectPermit:        not reconstructed/reused
old EffectAttempt:       historical only
```

The only way to mutate again is the ordinary fresh path:

```text
new invocation
→ new human confirmation
→ new ApprovalGrant
→ new EffectAttempt/effect_id
→ current RecoveryGuard check
→ normal M5 authority protocol
```

Reconciliation restores **eligibility**, never execution permission.

---

## 13. RecoveryGuard semantics after M6

RecoveryGuard remains process-local enforcement derived from durable truth.

Required behavior:

1. startup hydration validates **both** durable ledgers before browser mutation is
   available;
2. every supported M5 mutation refreshes the composite projection before the
   policy pass, preserving the M5 same-process stale-cache fix;
3. if either ledger cannot be read/validated, cached clear state is discarded
   and mutation fails closed;
4. only `UNRESOLVED_UNKNOWN` contributes a semantic replay block;
5. terminal reconciliation may remove that block only after durable append;
6. health/status reporting is diagnostic and never substitutes for the enforce
   path;
7. invocation-journal content cannot create, clear, or modify a recovery block.

If multiple unresolved effects share one semantic key, reconciling only one does
not clear the key while another unresolved effect remains.

---

## 14. Failure and crash semantics

### 14.1 Crash before reconciliation append

No durable resolution exists. RecoveryGuard remains blocking.

### 14.2 Reconciliation bytes written but fsync reports failure

The resolution is durability-ambiguous. The runtime must not publish it as
clear. Exact-fact retry uses the same `reconciliation_id` and content to
re-establish durability without a duplicate row.

### 14.3 Durable reconciliation, crash before guard refresh

Restart reads both ledgers and derives the reconciled disposition. Safety does
not depend on an in-memory post-append update.

### 14.4 Guard refresh fails after durable reconciliation

The durable resolution remains true, but current-process mutation stays
fail-closed until a later refresh successfully re-establishes composite recovery
truth.

### 14.5 Corrupt ReconciliationLedger

RecoveryGuard is unavailable. Do not fall back to EffectLedger-only projection,
because doing so could re-block or clear a key contrary to a durable
reconciliation fact whose history can no longer be trusted.

### 14.6 Corrupt EffectLedger

Existing M5 rule is unchanged: recovery fails closed.

### 14.7 Invocation journal failure/corruption

No effect on M6 authority.

---

## 15. Operator workflow

M6 initially supports a narrow local recovery workflow rather than a general CLI
platform.

Conceptual commands:

```text
webwire recovery list
webwire recovery show <effect_id>
webwire recovery inspect <effect_id>
webwire recovery resolve <effect_id> --verdict confirmed-effect
webwire recovery resolve <effect_id> --verdict confirmed-no-effect
```

The exact CLI syntax is implementation-layer detail, but the authority semantics
are frozen:

- `list/show/inspect` are read-only;
- `inspect` may collect bounded evidence and may remain inconclusive;
- `resolve` presents effect lineage, proposed verdict, and evidence summary to the
  human operator;
- terminal resolution requires explicit interactive confirmation in the same
  local session;
- a model/capability cannot invoke a hidden non-interactive bypass through the
  normal Dispatcher surface;
- `resolve` does not start the browser mutation stack or perform compensating
  external writes;
- after resolution, a separate normal invocation is required for any mutation.

Because M5/M6 are single-process, the supported operator workflow assumes
exclusive WireAgent process ownership. Concurrent independent recovery/runtime
processes are not a supported deployment mode.

---

## 16. Windows durability qualification

M5 models Windows file durability honestly but current CI evidence is Ubuntu.
M6 must qualify, not merely restate, the target-platform claim.

Windows qualification covers both EffectLedger and ReconciliationLedger:

- directory/state creation behavior;
- append and writable-handle flush behavior;
- exact-fact re-durability after simulated ambiguous flush failure;
- truncated/corrupt tail fail-closed behavior;
- restart projection from durable files;
- parent directory behavior documented according to what Python/Windows actually
  exposes;
- no stronger persistence claim than the evidence supports.

If Windows testing falsifies an assumption, the design/runtime is revised. If it
does not, the result is recorded as bounded qualification evidence rather than a
universal filesystem guarantee.

---

## 17. Replay-safety qualification after reconciliation

M6 may investigate conservative M5 replay classifications, but policy promotion
is evidence-driven and independent from reconciliation completion.

Like/unlike remain:

```text
ReplaySemantics.UNKNOWN
DurabilityPolicy.REQUIRED
```

until the concrete broker surface proves directional state-set behavior at least
across:

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

A higher-level pre-state check is not enough. Promotion to `SAFE_STATE_SET` /
`BEST_EFFORT` is permitted only after concrete broker-level implementation and
regression evidence establish the local claim.

A failed qualification is useful evidence: the conservative M5 policy remains
unchanged and the negative result is retained.

---

## 18. Frozen M6 invariants

1. M5 EffectLedger history is never rewritten by reconciliation.
2. `EFFECT_UNKNOWN` remains terminal historical M5 state.
3. Reconciliation is a separate durable evidence/authority axis.
4. Only raw `RESERVED` or `EFFECT_UNKNOWN` effects are valid reconciliation
   targets.
5. Reconciliation lineage must exactly match the canonical M5 effect lineage.
6. Terminal reconciliation has exactly two verdicts:
   `CONFIRMED_EFFECT` and `CONFIRMED_NO_EFFECT`.
7. Inconclusive evidence appends no recovery-authoritative terminal fact.
8. Every terminal reconciliation carries non-empty structured evidence and an
   explicit operator identity.
9. Evidence collectors may propose; they may not commit reconciliation truth.
10. A live in-process attempt cannot be reconciled while it can still emit an
    M5 terminal outcome.
11. Reconciliation durability precedes any in-memory unblock.
12. Reconciliation append/fsync ambiguity remains blocked until exact-fact
    durability is established.
13. Contradictory terminal reconciliation is corruption, not last-row-wins.
14. Recovery enforcement validates both durable ledgers and fails closed if
    either authority source is corrupt/unavailable.
15. Multiple unresolved effects sharing a semantic key keep that key blocked
    until every unresolved effect is settled/reconciled.
16. Reconciliation clears uncertainty eligibility only; it never revives old
    confirmation, grant, attempt, claim, or permit authority.
17. A post-reconciliation mutation is always a fresh normal M5 invocation.
18. A failed/missing observation is never generic proof of no effect.
19. Invocation journal content has zero reconciliation/recovery authority.
20. M6 makes no new cross-process linearizability or exactly-once claim.
21. Replay-safety policy promotion requires concrete broker-level evidence and
    is not implied by successful reconciliation work.
22. Platform qualification claims remain bounded to the environment actually
    tested.

---

## 19. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| R1 | Raw `EFFECT_UNKNOWN`, no reconciliation | Matching semantic key blocked |
| R2 | Raw `RESERVED` after restart, no reconciliation | Effective unknown; matching semantic key blocked |
| R3 | `CONFIRMED_EFFECT` durably reconciles `EFFECT_UNKNOWN` | Composite disposition `RECONCILED_EFFECT`; recovery block removed only after fsync |
| R4 | `CONFIRMED_NO_EFFECT` durably reconciles `EFFECT_UNKNOWN` | Composite disposition `RECONCILED_NO_EFFECT`; recovery block removed only after fsync |
| R5 | Either terminal verdict reconciles raw `RESERVED` with no live attempt | Same corresponding reconciled disposition |
| R6 | Inspection/evidence is inconclusive | No terminal reconciliation row; block remains |
| R7 | Unknown `effect_id` | Reconciliation denied; no row |
| R8 | Any copied lineage field differs | Reconciliation denied/fail-closed; no row |
| R9 | Target EffectLedger state is already `NO_EFFECT` or `EFFECT_CONFIRMED` | Reconciliation denied as invalid target |
| R10 | Live nonterminal attempt owns target `effect_id` | Reconciliation denied `live_attempt_owned` |
| R11 | Evidence missing/empty/non-JSON | Reconciliation denied before append |
| R12 | Operator authority absent/mismatched to effect/verdict/evidence | Reconciliation denied |
| R13 | Reconciliation append/fsync fails before durable success | Guard remains blocked; no clear publication |
| R14 | Bytes visible but fsync reports failure | Same-fact retry re-establishes durability without duplicate row; block stays until retry succeeds |
| R15 | Durable reconciliation succeeds then process crashes before guard refresh | Restart derives reconciled disposition from both ledgers |
| R16 | Second same terminal fact is exact retry | Re-fsync allowed; no duplicate row |
| R17 | Second terminal verdict contradicts first | Corruption/fail-closed; never last-row-wins |
| R18 | ReconciliationLedger malformed/torn/corrupt | RecoveryGuard unavailable; mutation denied |
| R19 | EffectLedger malformed/torn/corrupt | Existing M5 fail-closed behavior preserved |
| R20 | Invocation journal contains forged matching write/reconciliation-looking data | No effect on projection or guard |
| R21 | Two unresolved effects share one semantic key; only one is reconciled | Semantic key remains blocked by the other effect |
| R22 | Both unresolved effects sharing a key are validly reconciled | Key clears after durable composite refresh |
| R23 | Reconciliation completes | Old ApprovalGrant/EffectPermit/confirmation token cannot be reused; fresh invocation required |
| R24 | Concurrent same-process terminal reconciliation attempts | At most one terminal fact wins; competitor sees settled/conflict state |
| R25 | Evidence collector proposes terminal verdict without operator authority | Cannot append reconciliation fact or clear guard |
| R26 | Browser read fails during inspection | Result is inconclusive/error, never automatic `CONFIRMED_NO_EFFECT` |
| R27 | Durable resolution exists but guard refresh fails | Current process stays fail-closed until refresh succeeds |
| R28 | Restart after `CONFIRMED_EFFECT` reconciliation | History remains unknown + reconciliation fact; guard is clear for a new approved invocation |
| R29 | Restart after `CONFIRMED_NO_EFFECT` reconciliation | Same: old authority stays dead; guard is clear for a new approved invocation |
| R30 | Windows durability qualification | Recorded evidence matches actual file/directory semantics; unsupported stronger claims are rejected |

Additional mandatory regressions:

- strict JSON validation for reconciliation evidence;
- genuine non-empty string validation for identities and lineage;
- reserved evidence keys cannot forge runtime correlation fields;
- same-path reconciliation writers serialize process-locally;
- projection read/publication cannot regress from a newer blocked snapshot to an
  older clear snapshot;
- a reconciliation for one `effect_id` cannot clear another unresolved effect
  with the same semantic key;
- no Dispatcher capability can obtain `ReconciliationAuthority`;
- read-only inspection performs no external mutation;
- operator identity is distinct from X actor identity;
- positive evidence policies never infer causal identity from content equality
  alone unless that exact predicate has been qualified;
- negative evidence policies never infer no-effect from failed observation;
- reconciliation file absence is valid empty history; corruption is not;
- audit-journal absence/corruption remains irrelevant to M6 recovery truth;
- M6 does not alter M5 monotonic grant/permit clock semantics;
- M6 does not weaken M5 kill/epoch/policy/permit validation.

---

## 20. Build order

```text
1. Reconciliation record model + fsync-backed ReconciliationLedger
2. Composite RecoveryProjector + RecoveryGuard integration
3. ReconciliationCoordinator + local operator authority/workflow
4. Fault/restart/corruption qualification of the complete reconciliation path
5. Windows durability qualification for effect + reconciliation ledgers
6. Evidence-driven replay-safety qualification (like/unlike first candidate)
```

Each layer uses the repository review sequence:

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

If independent review tooling is unavailable because of quota or service limits,
a distinct adversarial second pass is performed and recorded explicitly rather
than treating missing review as approval.

---

## 21. M6 completion criteria

M6 is complete only when all of the following are true:

```text
✓ historical M5 uncertainty remains immutable
✓ terminal reconciliation is separately durable and lineage-bound
✓ evidence cannot silently become authority
✓ unresolved effects remain blocked under inconclusive/missing evidence
✓ durable reconciliation clears uncertainty only after fsync
✓ old execution authority is never restored
✓ restart recomputes the same composite recovery truth
✓ corruption in either safety ledger fails closed
✓ operator workflow exists without a normal capability bypass
✓ Windows durability claims have actual platform evidence
✓ like/unlike policy is either evidence-promoted or deliberately remains conservative
```

M6 does not claim cross-process coordination. If WireAgent later supports
multiple independent runtime/recovery processes sharing one state directory,
that is an M7 forcing function requiring a separate authority/locking design.
