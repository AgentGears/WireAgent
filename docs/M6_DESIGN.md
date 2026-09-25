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
> not rewrite the original effect fact. Reconciliation may restore eligibility
> for a new invocation, but it never restores old execution authority.**

---

## 1. Problem statement

M5 deliberately stops automatic replay when a REQUIRED effect is durably
uncertain. That is correct fail-closed behavior, but durable uncertainty cannot
remain operationally permanent.

Two M5 raw states require later human/operator resolution:

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

M6 introduces that recovery boundary while preserving the M5 laws that made
uncertainty safe.

### 1.1 Dangerous shortcuts M6 rejects

M6 must not:

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
- trust a reconciliation row whose append reported durability failure merely
  because its bytes are visible;
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
visible bytes      != known durable fact
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
| **RecoveryProjector** | Validated join of EffectLedger and ReconciliationLedger into current recovery truth. |
| **RecoveryGuard** | Process-local enforcement cache/gate derived from the composite recovery projection. |
| **ReconciliationAuthority** | Narrow local authority bound to one immutable reconciliation fact after explicit operator confirmation. |
| **EvidenceCollector** | Read-only mechanism that gathers bounded observations and may propose a verdict; it cannot commit one. |
| **Fresh invocation** | A new normal capability execution with new human confirmation and new M5 grant/attempt lineage. |
| **Live attempt ownership** | A current in-process M5 attempt still capable of writing a terminal outcome for the target effect. Reconciliation may not race it. |
| **Durability-ambiguous reconciliation** | A complete reconciliation row may be visible after append reported fsync/directory-sync failure; it is not recovery authority until re-durability succeeds. |

---

## 5. Why reconciliation is a separate ledger

M6 does not add successors to the M5 `EffectState` machine.

M5 remains:

```text
RESERVED -> NO_EFFECT | EFFECT_CONFIRMED | EFFECT_UNKNOWN
EFFECT_UNKNOWN is terminal
```

Reconciliation answers a different question:

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

This separation ensures:

1. M5 history remains readable under its frozen schema and state machine.
2. A reconciliation bug cannot masquerade as an original execution outcome.
3. Evidence provenance remains distinguishable from effect provenance.
4. Recovery can be recomputed from two append-only histories without rewriting
   either one.

The invocation journal is neither ledger and remains irrelevant to recovery
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
the effect remains unresolved and RecoveryGuard remains blocking. Audit may
record an inconclusive inspection, but audit does not alter recovery authority.

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
an indefinitely persisted prohibition on a future newly approved identical
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
  evidence_hash:      lowercase SHA-256 hex of canonical evidence JSON
  evidence:           non-empty strict-JSON object
  timestamp:          UTC wall-clock provenance string
```

### 7.1 Lineage equality

For a reconciliation to be valid, every copied lineage field must exactly equal
the canonical first EffectLedger record for the same `effect_id`.

The target EffectLedger history must currently project unresolved from raw:

```text
RESERVED | EFFECT_UNKNOWN
```

A reconciliation record for an unknown effect, a terminal M5 effect, or changed
lineage is invalid.

### 7.2 Evidence requirements and canonical hash

`evidence` is strict portable JSON under the same no-coercion rule as M5 durable
evidence. At minimum it contains:

```text
basis:          non-empty project-defined string
observed_at:    UTC provenance string
observations:   non-empty list of structured observations
```

`evidence_hash` is computed over one canonical JSON encoding:

```text
UTF-8
sorted object keys
compact separators
finite JSON numbers only
no non-JSON coercion
SHA-256 -> lowercase hexadecimal
```

The record is invalid if the stored hash does not match the canonical evidence
bytes. ReconciliationAuthority binds to this hash so evidence cannot change
after the operator approves a verdict.

Optional external artifacts carry content identity, not only a mutable path:

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

`operator_id` identifies the local human/operator authority committing the
resolution. It is not the X actor identity and must not be inferred from
`actor_id`.

M6 remains single-user/local, so this is provenance rather than multi-user RBAC.

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
evidence_hash
evidence
```

Timestamp is provenance and may be ignored only for same-fact re-durability,
matching the M5 exact-fact retry principle.

### 8.1 Durability

`ReconciliationLedger` uses the M5 safety posture:

- append-only NDJSON;
- owner-writable state file;
- full write loop;
- file `fsync` before success;
- parent-directory fsync for newly created entries where the platform exposes
  that primitive;
- process-local same-path writer serialization;
- read/schema/history corruption fails closed;
- exact-fact re-durability after ambiguous append failure.

### 8.2 Durability-ambiguity latch

A reconciliation fact differs from an ambiguous `RESERVED` fact in one crucial
way: trusting a visible but not known-durable `CONFIRMED_NO_EFFECT` row could
**remove** a safety block. Visibility therefore cannot stand in for durability.

For every normalized ReconciliationLedger path, the supported process maintains
a shared durability-ambiguity latch for any fact whose append wrote/possibly
wrote bytes but did not report complete file + required directory durability.

Required behavior:

```text
append reports durability ambiguity
→ exact reconciliation fact becomes locally AMBIGUOUS
→ composite recovery projection for that path is unavailable/fail-closed
→ visible row MUST NOT clear RecoveryGuard
→ exact-fact re-durability re-fsyncs file/directory as applicable
→ only successful re-durability clears the AMBIGUOUS latch
→ projector may then use the reconciliation fact
```

A second ReconciliationLedger instance for the same normalized path shares the
same process-local ambiguity state, just as same-path writer locking is shared.

After process restart, in-memory ambiguity state is gone. Before startup recovery
trusts an existing reconciliation file, the supported reader establishes current
file durability with a writable-handle fsync (and parent-directory fsync where
applicable) before publishing reconciliation authority. If this startup
re-durability step fails, recovery is unavailable/fail-closed.

A terminal reconciliation therefore cannot affect RecoveryGuard merely because
its bytes became visible.

---

## 9. Evidence semantics and claim ceiling

Reconciliation evidence must support the specific claim about the specific
effect. M6 does not treat plausible state match as causal proof.

Examples:

```text
matching post text exists
    != this exact unknown attempt created it

object currently absent
    != this exact attempt produced no effect

current bookmark state is set
    != proof of which actor/process set it
```

An action-specific evidence policy may prove more only after that predicate is
explicitly designed and regression-qualified.

### 9.1 Positive evidence

Positive remote identity can be strong evidence when it uniquely binds to the
approved effect lineage, for example exact remote object identity plus actor,
target, and content proof generated by a qualified action-specific reconciler.

If the observation cannot distinguish the unknown attempt from a pre-existing or
externally created equivalent object, the collector must report ambiguity. It
may not upgrade correlation into causal identity.

### 9.2 Negative evidence

Negative evidence has a higher claim burden. Read failure is not evidence of
absence, and observed absence is not automatically proof that no effect ever
occurred.

`CONFIRMED_NO_EFFECT` therefore requires either:

1. an action-specific conclusive no-effect predicate explicitly accepted and
   regression-qualified; or
2. explicit operator resolution with basis and limitations preserved in the
   durable evidence object.

### 9.3 Evidence collector boundary

An `EvidenceCollector` may:

- read canonical effect lineage;
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
- binds exactly one immutable tuple:

```text
effect_id
verdict
evidence_hash
operator_id
```

- cannot mint ApprovalGrant or EffectPermit;
- cannot mutate EffectLedger history;
- can only request the ReconciliationCoordinator to persist the bound fact.

Programmatic non-interactive model-authorized reconciliation is out of scope for
M6.

### 10.1 One authority, one fact, potentially multiple durability attempts

“Single-use” means the authority may authorize only one immutable reconciliation
**fact**. It does not mean one filesystem syscall.

Before the first durability call, the coordinator freezes the complete
ReconciliationRecord and verifies that its evidence hash matches the approved
authority.

If persistence reports ambiguity, that same authority lineage may re-drive
**only the exact same frozen fact** until durability is established or the
operator abandons the process. It may not change verdict, evidence, lineage, or
operator identity.

Once durable success is established, the authority is consumed and cannot be
used for any other effect/fact.

This preserves bounded human authority without making fsync ambiguity
unrecoverable.

### 10.2 Reconciliation is recovery-only, not in-flight rescue

M6 may not reconcile an effect while a live in-process M5 attempt still owns that
`effect_id` and could append its own terminal EffectLedger outcome.

```text
live nonterminal attempt owns effect_id
    -> reconciliation denied: live_attempt_owned
```

After restart, ephemeral attempts/grants are gone; stale durable `RESERVED`
becomes eligible for operator reconciliation under the normal single-process
ownership assumption.

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
→ canonicalize evidence and verify evidence_hash
→ read and fully validate/re-durable ReconciliationLedger
→ require no terminal reconciliation already exists
→ validate operator authority binding
→ freeze complete ReconciliationRecord
→ append exact ReconciliationRecord
→ fsync ReconciliationLedger (+ required parent durability)
→ clear any local ambiguity latch only after complete durability succeeds
→ consume reconciliation authority for that frozen fact
→ only then refresh composite RecoveryProjector/RecoveryGuard
→ return durable reconciliation result
```

No clear/release occurs before fsync-backed reconciliation durability.

If the append reports ambiguity, the coordinator retains the frozen fact and its
bounded authority lineage only for exact-fact re-durability. Recovery remains
fail-closed until that succeeds.

### 11.1 Concurrency

Within the supported single process:

- reconciliation operations are serialized by one protocol lock;
- competing terminal verdicts for one effect result in at most one durable
  winner;
- terminal M5 outcome writing and reconciliation do not race for the same live
  effect because live-attempt ownership is a hard precondition;
- composite projection reads validated ledger histories in one documented order
  and publishes only a complete joined snapshot;
- an older clear snapshot may never overwrite a newer blocked/unavailable
  snapshot;
- same-path reconciliation ledger instances share writer lock and ambiguity
  state.

Independent external processes are not coordinated and remain out of scope
until M7.

---

## 12. Composite recovery projection

M6 moves enforcement projection into a join component:

```text
EffectLedger -----------\
                         -> RecoveryProjector -> RecoveryGuard
ReconciliationLedger ---/
```

`EffectLedger.recovery_projection()` may remain a raw M5 diagnostic/helper, but
M6 enforcement uses the composite projector.

For each effect:

| Raw M5 state | Reconciliation | Composite disposition | Guard behavior |
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
already-settled M5 effect, is corruption and makes recovery authority
unavailable.

A durability-ambiguous reconciliation is not a valid row for projection even if
its bytes are visible; the projector is unavailable until re-durability clears
the per-path ambiguity latch.

### 12.1 Clearing recovery is not replaying the old attempt

After either terminal reconciliation verdict:

```text
old confirmation token: invalid/expired/not reconstructed
old ApprovalGrant:       not reconstructed; if still present it is not reopened
old EffectPermit:        not reconstructed/reused
old EffectAttempt:       historical only
```

The only way to mutate again is:

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

1. startup hydration validates both durable ledgers before browser mutation is
   available;
2. existing reconciliation history is re-durability-fsynced before it may clear
   a recovery block in the new process;
3. every supported M5 mutation refreshes the composite projection before the
   policy pass, preserving the M5 same-process stale-cache fix;
4. if either ledger cannot be read/validated/re-durability-established, cached
   clear state is discarded and mutation fails closed;
5. only `UNRESOLVED_UNKNOWN` contributes a semantic replay block;
6. terminal reconciliation may remove that block only after known durability;
7. health/status reporting is diagnostic and never substitutes for enforcement;
8. invocation-journal content cannot create, clear, or modify a recovery block.

If multiple unresolved effects share one semantic key, reconciling only one does
not clear the key while another unresolved effect remains.

---

## 14. Failure and crash semantics

### 14.1 Crash before reconciliation append

No durable resolution exists. RecoveryGuard remains blocking.

### 14.2 Reconciliation bytes written but fsync reports failure

The fact is locally durability-ambiguous. Its bytes may be visible, but the
composite projector is unavailable/fail-closed for that path. Exact-fact retry
uses the same frozen record/authority lineage to re-establish durability without
a duplicate row.

### 14.3 Durable reconciliation, crash before guard refresh

Restart reads both ledgers, re-establishes current reconciliation-file durability,
and derives the reconciled disposition. Safety does not depend on an in-memory
post-append update.

### 14.4 Guard refresh fails after durable reconciliation

The durable resolution remains true, but current-process mutation stays
fail-closed until a later refresh successfully re-establishes composite recovery
truth.

### 14.5 Corrupt ReconciliationLedger

RecoveryGuard is unavailable. Do not fall back to EffectLedger-only projection,
because doing so could block/clear contrary to reconciliation history that can no
longer be trusted.

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

Exact syntax is implementation detail, but authority semantics are frozen:

- `list/show/inspect` are read-only;
- `inspect` may collect bounded evidence and may remain inconclusive;
- `resolve` presents effect lineage, proposed verdict, canonical evidence hash,
  and evidence summary to the human operator;
- terminal resolution requires explicit interactive confirmation in the same
  local session;
- a model/capability cannot invoke a hidden non-interactive bypass through the
  normal Dispatcher surface;
- `resolve` does not perform compensating external writes;
- after resolution, a separate normal invocation is required for any mutation.

Because M5/M6 are single-process, the supported operator workflow assumes
exclusive WireAgent process ownership. Concurrent independent recovery/runtime
processes are unsupported.

---

## 16. Windows durability qualification

M5 models Windows file durability honestly but current CI evidence is Ubuntu.
M6 must qualify, not merely restate, the target-platform claim.

Windows qualification covers EffectLedger and ReconciliationLedger:

- directory/state creation behavior;
- append and writable-handle flush behavior;
- exact-fact re-durability after simulated ambiguous flush failure;
- reconciliation ambiguity-latch behavior;
- startup reconciliation-file re-durability before projection;
- truncated/corrupt tail fail-closed behavior;
- restart projection from durable files;
- parent-directory behavior documented according to what Python/Windows actually
  exposes;
- no stronger persistence claim than evidence supports.

If Windows testing falsifies an assumption, design/runtime is revised. Otherwise
the result is bounded qualification evidence, not a universal filesystem
guarantee.

---

## 17. Replay-safety qualification after reconciliation

M6 may investigate conservative M5 replay classifications, but policy promotion
is evidence-driven and independent from reconciliation completion.

Like/unlike remain:

```text
ReplaySemantics.UNKNOWN
DurabilityPolicy.REQUIRED
```

until the concrete broker surface proves directional state-set behavior across
at least:

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
`BEST_EFFORT` is permitted only after concrete broker implementation and
regression evidence establish the local claim.

A failed qualification is useful evidence: conservative M5 policy remains
unchanged and the negative result is retained.

---

## 18. Frozen M6 invariants

1. M5 EffectLedger history is never rewritten by reconciliation.
2. `EFFECT_UNKNOWN` remains terminal historical M5 state.
3. Reconciliation is a separate durable evidence/authority axis.
4. Only raw `RESERVED` or `EFFECT_UNKNOWN` effects are valid reconciliation
   targets.
5. Reconciliation lineage exactly matches canonical M5 effect lineage.
6. Terminal reconciliation has exactly two verdicts:
   `CONFIRMED_EFFECT` and `CONFIRMED_NO_EFFECT`.
7. Inconclusive evidence appends no recovery-authoritative terminal fact.
8. Every terminal reconciliation carries non-empty strict evidence, canonical
   evidence hash, and explicit operator identity.
9. Evidence collectors may propose; they may not commit reconciliation truth.
10. ReconciliationAuthority binds one immutable effect/verdict/evidence-hash fact
    and may re-drive only that fact through durability ambiguity.
11. A live in-process attempt cannot be reconciled while it can still emit an
    M5 terminal outcome.
12. Known reconciliation durability precedes any in-memory unblock.
13. Visible-but-ambiguous reconciliation bytes never clear recovery authority.
14. Same-path ledger instances share local ambiguity state; exact re-durability
    is the only in-process way to clear it.
15. Startup establishes current reconciliation-file durability before using
    existing rows to clear a block.
16. Contradictory terminal reconciliation is corruption, not last-row-wins.
17. Recovery validates both durable ledgers and fails closed if either authority
    source is corrupt/unavailable/ambiguous.
18. Multiple unresolved effects sharing a semantic key keep that key blocked
    until every unresolved effect is settled/reconciled.
19. Reconciliation clears uncertainty eligibility only; it never revives old
    confirmation, grant, attempt, claim, or permit authority.
20. A post-reconciliation mutation is always a fresh normal M5 invocation.
21. Failed/missing observation is never generic proof of no effect.
22. Invocation journal content has zero reconciliation/recovery authority.
23. M6 makes no new cross-process linearizability or exactly-once claim.
24. Replay-safety policy promotion requires concrete broker-level evidence and
    is not implied by successful reconciliation work.
25. Platform qualification claims remain bounded to the environment actually
    tested.

---

## 19. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| R1 | Raw `EFFECT_UNKNOWN`, no reconciliation | Matching semantic key blocked |
| R2 | Raw `RESERVED` after restart, no reconciliation | Effective unknown; matching semantic key blocked |
| R3 | `CONFIRMED_EFFECT` durably reconciles `EFFECT_UNKNOWN` | `RECONCILED_EFFECT`; block removed only after known durability |
| R4 | `CONFIRMED_NO_EFFECT` durably reconciles `EFFECT_UNKNOWN` | `RECONCILED_NO_EFFECT`; block removed only after known durability |
| R5 | Either verdict reconciles raw `RESERVED` with no live attempt | Same corresponding reconciled disposition |
| R6 | Inspection/evidence is inconclusive | No terminal reconciliation row; block remains |
| R7 | Unknown `effect_id` | Denied; no row |
| R8 | Any copied lineage field differs | Denied/fail-closed; no row |
| R9 | Target M5 state already `NO_EFFECT` or `EFFECT_CONFIRMED` | Denied as invalid target |
| R10 | Live nonterminal attempt owns target `effect_id` | Denied `live_attempt_owned` |
| R11 | Evidence missing/empty/non-JSON/hash mismatch | Denied before append |
| R12 | Operator authority absent/mismatched to effect/verdict/evidence hash | Denied |
| R13 | Append/fsync fails before known durability | Per-path ambiguity latched; guard cannot clear |
| R14 | Complete bytes visible after reported fsync failure | Projector remains unavailable; exact-fact re-durability required |
| R15 | Exact-fact re-durability succeeds | Ambiguity latch clears; no duplicate row; fact may enter projection |
| R16 | Durable reconciliation succeeds then crash precedes guard refresh | Restart derives reconciled disposition after startup durability establishment |
| R17 | Second same terminal fact is exact retry | Re-fsync allowed; no duplicate row |
| R18 | Second terminal verdict contradicts first | Corruption/fail-closed; never last-row-wins |
| R19 | ReconciliationLedger malformed/torn/corrupt | RecoveryGuard unavailable; mutation denied |
| R20 | EffectLedger malformed/torn/corrupt | Existing M5 fail-closed behavior preserved |
| R21 | Invocation journal contains forged matching write/reconciliation-looking data | No effect on projection or guard |
| R22 | Two unresolved effects share one semantic key; only one reconciled | Key remains blocked by other effect |
| R23 | All unresolved effects sharing key are validly reconciled | Key clears after durable composite refresh |
| R24 | Reconciliation completes | Old grant/permit/confirmation cannot be reused; fresh invocation required |
| R25 | Concurrent same-process terminal reconciliation attempts | At most one terminal fact wins |
| R26 | Evidence collector proposes verdict without operator authority | Cannot append fact or clear guard |
| R27 | Browser read fails during inspection | Inconclusive/error; never automatic `CONFIRMED_NO_EFFECT` |
| R28 | Durable resolution exists but guard refresh fails | Process stays fail-closed until refresh succeeds |
| R29 | Restart after `CONFIRMED_EFFECT` | Unknown history + reconciliation preserved; fresh approved invocation eligible |
| R30 | Restart after `CONFIRMED_NO_EFFECT` | Same; old authority dead; fresh approved invocation eligible |
| R31 | Second ledger instance opens same path after ambiguous append | Shared ambiguity state prevents clear |
| R32 | Process restarts with complete surviving reconciliation row | Startup fsync/re-durability succeeds before row may clear guard |
| R33 | Startup re-durability of reconciliation file fails | Recovery unavailable/fail-closed |
| R34 | Reconciliation authority retries after ambiguous append | Only exact frozen fact accepted; changed verdict/evidence rejected |
| R35 | Windows durability qualification | Recorded claim matches actual file/directory semantics; stronger unsupported claim rejected |

Additional mandatory regressions:

- strict JSON validation for reconciliation evidence;
- canonical evidence hash is deterministic and mismatch is rejected;
- genuine non-empty string validation for identities and lineage;
- reserved evidence keys cannot forge runtime correlation fields;
- same-path reconciliation writers serialize process-locally;
- projection read/publication cannot regress from a newer blocked/unavailable
  snapshot to an older clear snapshot;
- reconciliation for one `effect_id` cannot clear another unresolved effect with
  the same semantic key;
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

If independent review tooling is unavailable because of quota/service limits, a
distinct adversarial second pass is performed and recorded explicitly rather
than treating missing review as approval.

---

## 21. M6 completion criteria

M6 is complete only when:

```text
✓ historical M5 uncertainty remains immutable
✓ terminal reconciliation is separately durable and lineage-bound
✓ evidence cannot silently become authority
✓ unresolved effects remain blocked under inconclusive/missing evidence
✓ visible-but-not-known-durable reconciliation can never clear a block
✓ durable reconciliation clears uncertainty only after durability succeeds
✓ old execution authority is never restored
✓ restart recomputes the same composite recovery truth
✓ corruption/ambiguity in either safety source fails closed
✓ operator workflow exists without a normal capability bypass
✓ Windows durability claims have actual platform evidence
✓ like/unlike policy is either evidence-promoted or deliberately remains conservative
```

M6 does not claim cross-process coordination. If WireAgent later supports
multiple independent runtime/recovery processes sharing one state directory,
that is an M7 forcing function requiring a separate authority/locking design.
