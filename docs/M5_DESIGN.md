# M5 — Effect Transaction Boundary (Design Specification)

```text
Status:   FROZEN FOR IMPLEMENTATION — REVISED BY IMPLEMENTATION EVIDENCE
Baseline: 7d081b9
Revision: layer-3 maintainer/Codex close-out, 2026-09-23
Change rule: revise only when implementation, fault injection, live evidence,
or an independently verified review finding falsifies an invariant or assumption.
```

This document is normative and self-contained. Build from this file, not from
review-conversation memory. `STATE.md` remains the living project record; during
staged migration it may still describe legacy live-runtime behavior that M5 has
not yet replaced.

Layer-3 implementation and independent review refined the original design in
ways that are now part of the contract:

- BEST_EFFORT can crash after mutation and before a terminal ledger append;
- approval claiming is a synchronized compare-and-set;
- issued-but-unused authority can expire after approval was spent;
- a durable append can write bytes and still report failure during fsync;
- one execution attempt therefore owns one stable effect identity;
- once REQUIRED reservation I/O starts, failure is no longer equivalent to a
  clean precommit failure;
- the safety ledger validates history, not merely record syntax;
- same-process safety serializes ledger validation + append;
- durable evidence is strict JSON and is never silently coerced;
- the mutable legacy `WriteIntent` is never re-read across a blocking commit
  sequence: the gateway captures one immutable private snapshot;
- target lineage must be non-empty and persistable before any reservation or
  execution authority can be created;
- approval validity, current authorization epoch, and kill state are checked at
  the final authority transition rather than trusted from an earlier sample;
- direct authorization-epoch changes are fenced with final mint/consume;
- approval validity and permit TTL use distinct process-local clock domains;
- grant and permit TTL defaults use monotonic elapsed time, not wall-clock time;
- permit TTL starts at actual permit mint and is sampled again at the actual
  consume transition after any blocking policy/epoch/kill validation;
- external hot-file kill state is re-observed at final mint/consume boundaries,
  without claiming impossible cross-process atomicity for a non-cooperating file
  writer;
- if a durable fenced reservation exists but approval becomes invalid or kill
  blocks before permit exposure, the reservation is closed `NO_EFFECT` before
  denial; failure to close remains unresolved/fail-closed;
- `SAFE_*` replay semantics remain evidence claims: bookmark/remove-bookmark are
  broker-regression-backed, while like/unlike remain `UNKNOWN`/`REQUIRED` until
  their concrete broker surface proves state-preserving replay safety.

---

## 1. Problem statement

WireAgent's pre-M5 write path has two verified architectural holes and one
conceptual conflation.

### Hole 1 — approved intent and execution authority are different scopes

A human can approve one semantic action while a capability receives the full
`WriteBroker`, whose surface contains unrelated mutations. Built-in
capabilities may behave correctly, but the architecture does not enforce least
execution authority.

### Hole 2 — replay-unsafe effects can escape durable safety state

The legacy write pipeline can produce an external public effect before its
best-effort invocation journal record exists. A crash in that interval can
leave no durable fact from which restart logic can safely suppress replay.

### Conflation — one risk axis cannot answer approval, authority, and retry

Impact, permitted semantic authority, replay behavior, and durability are
separate questions. `post` and `delete_post` can share a high impact tier while
having different replay behavior; a lower-risk state-setting action can still
be unsafe to replay if its concrete broker implementation is a toggle.

M5 introduces one architecture around those facts:

- registry-owned `EffectPolicy`;
- ephemeral `ApprovalGrant` and `EffectAttempt` state machines;
- a process-local `CommitGateway`;
- single-use `EffectPermit` authority;
- fsync-backed `EffectLedger` safety facts;
- scoped broker authorities in layer 4;
- live capability migration in layer 5;
- restart enforcement through `RecoveryGuard` in layer 6.

---

## 2. What M5 does not guarantee

- **No distributed exactly-once guarantee.** WireAgent and X do not share a
  transaction manager. REQUIRED effects target at-most-once *automatic*
  execution plus explicit reconciliation after durable uncertainty.
- **No hard sandbox for hostile same-process Python.** Scoped authorities are a
  least-authority engineering boundary for trusted same-process code. Untrusted
  extensions require process/OS isolation and no raw browser escape path.
- **No automatic reconciliation in this milestone.** Layer 6 must enforce the
  unresolved-effect deny gate. Automated external reconciliation can come
  later.
- **No persisted ApprovalGrant authority.** Approval grants are deliberately
  ephemeral; durable external-effect knowledge belongs to `EffectLedger`.
- **No claim that a missing BEST_EFFORT record proves no effect.** BEST_EFFORT
  omits the pre-mutation reservation by design. A crash can therefore leave no
  M5 fact; replay is allowed only because replay safety is a positive,
  test-backed policy claim.
- **No DOM-behavior-as-contract shortcut.** Replay safety must be implemented
  semantically and regression-tested against the real broker surface.
- **No cross-process ledger-writer coordination in the current runtime.** M5
  layer 3 is a single-process design. A future multi-process deployment needs
  OS/file/database coordination stronger than the current process-local lock.
- **No strict cross-process linearization for an external kill-file writer.**
  In-process `KillSwitch.trip()` participates in the state lock and has a strict
  process-local ordering with authority crossing. A separate process that merely
  creates `.webwire/kill` cannot share that Python lock. The gateway therefore
  re-observes the file at the final mint/consume boundary, closing long stale
  windows without claiming atomicity across the final filesystem observation and
  the following in-process transition. A stronger guarantee requires a
  cooperating cross-process locking/protocol mechanism.

---

## 3. Terminology

| Term | Meaning |
|---|---|
| **WriteIntent** | Existing mutable declarative action object: action, target, payload, risk metadata, semantic variant, actor. M5 never treats later reads of the caller-owned object as authority. |
| **IntentSnapshot** | Private immutable gateway snapshot: action, target, actor, semantic key, and canonical intent hash captured once for the commit sequence. |
| **ApprovalGrant** | Ephemeral record of one human approval, bound to intent, actor, target, policy identity, and authorization epoch. |
| **EffectAttempt** | One execution try against a grant. Owns stable `attempt_id`, stable `effect_id`, state, and reservation-start latch. |
| **EffectPermit** | Single-use process-local execution authority descended from one grant/attempt/snapshot lineage. |
| **CommitGateway** | Process-local authority boundary that validates current state and mints/consumes permits. |
| **EffectLedger** | Fsync-backed append-only M5 safety ledger at `.webwire/effects.ndjson`. |
| **EffectPolicy** | Registry declaration of risk, semantic effect scope, replay semantics, and derived durability. |
| **Authorization epoch** | Monotonic process-local revocation generation bound into grants and permits. |
| **Fenced effect** | `DurabilityPolicy.REQUIRED`; durable `RESERVED` must precede mutation authority. |
| **Non-fenced effect** | `BEST_EFFORT`; no precommit reservation, allowed only for proven replay-safe effects. |

---

## 4. EffectPolicy — independent axes

```text
EffectPolicy
├── risk                 # impact / approval dimension
├── allowed_effects      # semantic authority dimension
├── replay_semantics     # delivery / retry dimension
└── durability           # REQUIRED | BEST_EFFORT
```

Durability is derived from risk + replay semantics. A capability author cannot
arbitrarily downgrade it.

### 4.1 ReplaySemantics

```text
SAFE_STATE_SET
SAFE_TARGET_DELETE
NON_IDEMPOTENT_CREATE
REPLAY_HAS_RESIDUAL_EFFECTS
UNKNOWN
```

Rules:

1. `UNKNOWN`, `NON_IDEMPOTENT_CREATE`, and
   `REPLAY_HAS_RESIDUAL_EFFECTS` require durable fencing.
2. Public amplifying or public-content risk requires fencing even if the
   operation is otherwise target-idempotent.
3. `SAFE_*` is a positive implementation claim and requires regression evidence
   against the concrete semantic broker behavior.
4. Explicit durable `EFFECT_UNKNOWN` is unresolved regardless of whether the
   policy is otherwise replay-safe. The BEST_EFFORT replay exception concerns
   only a crash that leaves **no durable effect fact**.

### 4.2 Initial registry truth

| Action | replay semantics | durability |
|---|---|---|
| post / reply / quote (+ media variants) | `NON_IDEMPOTENT_CREATE` | `REQUIRED` |
| delete_post | `SAFE_TARGET_DELETE` | `REQUIRED` (consequential) |
| like / unlike | `UNKNOWN` | `REQUIRED` until broker-level state-set semantics are proven |
| bookmark / remove_bookmark | `SAFE_STATE_SET` | `BEST_EFFORT` |
| follow / unfollow | `UNKNOWN` | `REQUIRED` until implemented and proven |
| repost / unrepost | `UNKNOWN` | `REQUIRED` until implemented and proven |

The bookmark classification is a positive broker-level claim: both directional
methods read semantic state first, already-satisfied is a zero-mutation result,
and concrete regressions cover selector/state topology. Like/unlike are kept
conservative even though `LikeCapability.execute()` performs a high-level
pre-state read: the concrete broker methods remain selector-first and do not yet
have equivalent broker-level state-preserving/coexistence regressions. Until
that evidence exists, their replay semantics are `UNKNOWN`, not inferred safe
from today's DOM behavior.

---

## 5. ApprovalGrant and EffectAttempt

### 5.1 ApprovalGrant

```text
state:      ACTIVE → SPENT | EXPIRED | REVOKED
claim:      claimed_by: Optional[attempt_id]   # orthogonal synchronized CAS
bindings:   intent_hash, actor_id, action_type, target,
            policy_binding, authorization_epoch
validity:   expires_at; max_precommit_attempts
```

Properties:

- `SPENT`, `EXPIRED`, and `REVOKED` are terminal.
- `claim()` is an actual synchronized compare-and-set.
- the gateway holds the grant claim fence across the authority-granting
  protocol;
- clean precommit failure may release the claim and keep approval `ACTIVE`;
- once approval has been spent, the claim is never released back into reusable
  approval;
- public binding/lifecycle fields are sealed against accidental direct
  mutation; lifecycle methods own transitions;
- persistence of approval authority is intentionally out of scope.

**Clock domain:** grant expiry is elapsed-time authority. The production default
is `time.monotonic()`, so wall-clock rollback/forward correction cannot extend
or prematurely expire a process-local approval. Tests and future runtimes may
inject a grant clock independent from the gateway permit clock; the two clock
values must never be compared directly. UTC wall time remains appropriate for
ledger/audit timestamps, not authority TTL enforcement.

### 5.2 EffectAttempt

```text
PREPARING
   ├──→ NO_EFFECT              # proven clean precommit or invalid pre-mint authority
   └──→ RESERVED               # REQUIRED fence durably established
            ├──→ NO_EFFECT     # known unused / invalid before mutation authority
            ├──→ EFFECT_CONFIRMED
            └──→ EFFECT_UNKNOWN
```

For BEST_EFFORT, an issued permit can terminalize `PREPARING → NO_EFFECT` on
unused expiry or `PREPARING → EFFECT_*` after permit consumption.

Each attempt additionally owns:

```text
attempt.effect_id            # stable for lifetime of attempt
attempt.reservation_started  # False → True only; REQUIRED path only
```

`effect_id` belongs to the attempt, **not** to each `authorize_commit()` call.
If a reservation append writes bytes and later reports fsync failure, retrying
that same attempt addresses the same durable fact.

`reservation_started` is latched **before** ledger I/O. Once true, generic
clean-precommit `mark_no_effect()` is forbidden because the runtime cannot prove
that no durable reservation exists merely because the append call raised.

### 5.3 Four no-effect situations

1. **Clean precommit:** no reservation I/O began and no mutation authority was
   granted. Claim may be released; grant can remain `ACTIVE` within retry budget.
2. **Unused issued authority:** permit existed but expired unconsumed. Approval
   is already `SPENT`; attempt closes `NO_EFFECT` without restoring approval.
3. **Ambiguous reservation failure before permit mint:** no mutation authority
   was minted, but reservation bytes may exist. Claim remains held; same attempt
   and `effect_id` must retry/reconcile.
4. **Durable reservation, then approval/epoch/kill invalid before permit mint:**
   no permit has been exposed, so no external mutation crossed the gateway.
   Append durable `NO_EFFECT` and terminalize without releasing the claim back
   into reusable approval. If that close fails, keep `RESERVED` unresolved.

`COMMIT_ATTEMPTED`, `submit_call_started`, and `submit_call_returned` may be
useful diagnostics but are not canonical safety states.

---

## 6. Unified spend and mint rule

> An ApprovalGrant is reusable only after clean, proven precommit `NO_EFFECT`.
> It becomes irrevocably spent when the gateway grants authority capable of the
> approved external effect. REQUIRED authority is exposed only after durable
> reservation and a final liveness/epoch/kill check; BEST_EFFORT authority is
> spent at process-local permit mint.

### 6.1 One immutable intent snapshot

The legacy `WriteIntent` remains mutable. `authorize_commit()` therefore
captures one private deep-copied snapshot **after housekeeping and before grant
validation**. The following all use that same snapshot and never re-read the
caller-owned intent:

- policy action selection;
- grant intent/actor/target validation;
- semantic dedupe key;
- durable reservation lineage;
- permit intent/actor/target/action fields.

Concurrent mutation of the caller-owned object after snapshot capture cannot
change the authority being minted. A torn or changed snapshot still has to match
the already-approved grant bindings before authority proceeds.

Before any durability branch, the gateway also requires `target_type` and
`target_id` to be genuine non-empty strings. This is an authority/lineage
validity check, not action-specific semantic validation: Layer 3 proves that a
permit's target can later be represented by the durable EffectLedger schema;
Layers 4–5 remain responsible for whether a particular target is a valid X post,
user, or other action-specific object.

### 6.2 REQUIRED protocol

Lock order for authorization:

```text
gateway protocol lock
  → kill execution fence
    → policy registry fence
      → grant claim fence
        → authorization-epoch fence at final mint
```

Permit consumption uses:

```text
gateway protocol lock
  → kill execution fence
    → policy registry fence
      → authorization-epoch fence at final consume
```

Slow REQUIRED ledger I/O remains outside the authorization-epoch fence so a
direct `AuthorizationEpoch.bump()` can proceed while fsync is blocked and then
be observed before authority is minted.

Protocol:

```text
prune old expired permits
→ capture immutable intent snapshot
→ validate current grant/snapshot/policy/epoch
→ reject missing/unpersistable target lineage
→ latch reservation_started
→ append exact RESERVED fact
→ fsync ledger (+ new directory entry where platform supports it)
→ mark attempt RESERVED
→ enter authorization-epoch fence
→ callback-free re-observe current kill state
→ revalidate grant liveness + fenced current authorization epoch
→ if still valid: spend grant
→ read gateway clock at actual mint
→ mint one permit with full TTL
```

The second grant validation is intentional: durable I/O can block long enough
for approval expiry or epoch revocation to become relevant before authority is
exposed. The final kill refresh similarly catches external hot-file activation
that occurred during the blocking work; it does not execute listener callbacks
inside the authority fence.

If post-reservation grant/epoch/kill validation fails:

```text
no permit exists
→ append RESERVED -> NO_EFFECT
→ terminalize attempt without restoring approval claim
→ deny the relevant expiry/revocation/kill reason
```

If the `NO_EFFECT` append itself fails, surface `prepermit_close_failed`, mint no
permit, and leave raw `RESERVED` unresolved/fail-closed.

If the original reservation append reports failure:

- no permit is minted;
- grant is not spent;
- claim remains held by the same attempt;
- `reservation_started` remains true;
- stable `effect_id` is retained;
- exact-fact retry may re-establish durability without appending a duplicate;
- generic clean-precommit release is forbidden.

### 6.3 BEST_EFFORT protocol

```text
prune old expired permits
→ capture immutable intent snapshot
→ validate grant/snapshot/policy/epoch
→ reject missing/unpersistable target lineage
→ enter authorization-epoch fence
→ callback-free re-observe current kill state
→ revalidate grant/epoch immediately before mint
→ spend grant
→ read gateway clock at actual mint
→ mint one permit
```

There is no precommit durable reservation. If final pre-mint validity fails,
no permit exists and no durable fence is needed; terminalize the attempt as
proven `NO_EFFECT` without reopening approval.

If the process survives a mutation, known confirmed/unknown outcomes are still
appended durably. A crash between remote mutation and terminal append relies on
proven replay safety, not a fictitious missing ledger fact.

### 6.4 Permit clock domain

`EffectPermit.issued_at` and `expires_at` use the gateway's permit clock and are
computed **at actual mint**. The production default is `time.monotonic()` because
the permit is a process-local elapsed-time capability:

```text
mint_now = gateway_monotonic_clock()
issued_at = mint_now
expires_at = mint_now + permit_ttl
```

Reservation fsync latency and expired-permit cleanup do not consume a newly
returned permit's TTL. System wall-clock correction cannot extend or prematurely
expire the permit. Injected clocks remain supported for deterministic tests and
special runtimes.

Permit validity is also sampled at the **actual consume transition**. An early
expiry check is only a fast path; policy/epoch/kill validation can block, so the
gateway reads its monotonic clock again after those checks and immediately
before setting the single-use consumed state. A permit that expires while
waiting is closed `NO_EFFECT` as unused authority rather than crossing on a
stale timestamp.

### 6.5 Spend is not reversed by permit expiry

Permit expiry proves only that the particular permit never crossed the mutation
boundary. Fenced expiry first persists `RESERVED → NO_EFFECT`; then the canonical
attempt terminalizes and permit lineage is evicted. The grant remains `SPENT`.

---

## 7. Authorization epoch and kill linearization

```text
authorization_epoch = N
ApprovalGrant.epoch = N
EffectPermit.epoch = N

new trip generation → authorization_epoch = N+1
old grant/permit     → invalid
```

`KillSwitch` uses generation-based listener obligations. The gateway's epoch
listener is critical. Once a trip generation is observed, `execution_fence()`
remains fail-closed while critical delivery is pending, even if an earlier
listener resets the visible switch.

Listener callbacks execute **outside** the kill-state lock. Re-entrant listeners
are tracked per listener/generation; reset+retrip cannot cause an old callback
to satisfy a newer generation.

`AuthorizationEpoch` has its own process-local fence. Slow reservation I/O does
not hold it; the gateway acquires it around final spend/mint and final permit
consumption. A direct `bump()` therefore either occurs before the final epoch
check and denies stale authority, or after the authority transition has already
won. The gateway does not trust an epoch value sampled earlier in the protocol.

Programmatic `KillSwitch.trip()` shares the kill-state lock with
`execution_fence()`, giving the in-process active-state transition a strict
process-local ordering with authority crossing. External hot-file creation is
different: a non-cooperating process cannot participate in that Python lock.
The gateway therefore performs a callback-free current-state refresh immediately
before final permit mint and consume. If the file appeared during slow
reservation/policy work, authority is denied; the observed generation remains
owed to critical listeners and normal later observation drains it. There is
still an unavoidable filesystem-observation-to-transition micro-window unless
external writers adopt a cooperating cross-process protocol, so M5 makes no
stronger cross-process linearizability claim.

---

## 8. Commit Gateway invariants

1. In the completed M5 path, every remote mutation crosses the gateway.
2. Claim ownership is synchronized and held through authority creation.
3. One attempt has one stable `effect_id`.
4. One private immutable intent snapshot supplies all commit-authority lineage.
5. REQUIRED reservation I/O is latched before the ledger call.
6. No REQUIRED mutation authority is minted until reservation durability
   succeeds.
7. Before any reservation or permit mint, target lineage is non-empty and
   representable by the durable ledger schema.
8. Approval liveness, current authorization epoch, and current kill state are
   revalidated at the final mint boundary; direct epoch changes are fenced with
   that authority transition.
9. A reservation failure cannot become generic clean precommit release.
10. A durable fenced reservation whose authority becomes invalid before permit
    exposure closes `NO_EFFECT`; close failure remains unresolved.
11. Permit TTL starts at actual permit mint and is sampled again at the actual
    consume transition after blocking authority checks.
12. Permit validation/consumption is atomic under the gateway protocol lock;
    policy identity, actor, target, intent, effect scope, epoch, kill state, and
    TTL must all still be valid at that boundary.
13. The gateway accepts only the exact permit object it issued and retains the
    exact canonical attempt object for terminal lifecycle mutation.
14. Durable outcome append precedes in-memory terminalization/eviction.

---

## 9. Scoped authorities (layer 4)

Layer 3 creates process-local permit authority; layer 4 must prevent a capability
from receiving the full mutation surface.

```text
broker.authorize(permit)
  → LikeAuthority | BookmarkAuthority | PostAuthority | DeleteAuthority | ...
```

Precommit composer operations versus the single-use irreversible submit permit
must be resolved in the scoped-authority adapter design. Layer 3 intentionally
does not claim that concrete capabilities already cross the new boundary.

Same-process scoping is an engineering boundary, not a malicious-code sandbox.

---

## 10. EffectLedger contract

Canonical states:

```text
NO_EFFECT
RESERVED
EFFECT_CONFIRMED
EFFECT_UNKNOWN
```

### 10.1 Canonical history

For one `effect_id`:

- semantic lineage is immutable: semantic key, action type, intent hash, policy
  binding, actor, target type, target id;
- a fenced effect starts at `RESERVED`;
- `RESERVED` may advance once to `NO_EFFECT`, `EFFECT_CONFIRMED`, or
  `EFFECT_UNKNOWN`;
- a replay-safe BEST_EFFORT effect may first appear as `EFFECT_CONFIRMED` or
  `EFFECT_UNKNOWN` because it has no precommit reservation;
- terminal states cannot be overwritten by another terminal state;
- contradictory history is corruption and recovery fails closed rather than
  choosing the last row.

An unresolved raw `RESERVED` projects to effective `EFFECT_UNKNOWN` after
restart without rewriting evidence.

### 10.2 Record schema

Durable identity fields are genuine non-empty strings; optional lineage fields
are either `None` or non-empty strings. Gateway execution authority uses
non-empty target type/id so a consumed permit can always be represented by the
terminal ledger schema. Evidence is strict portable JSON: strings, booleans,
integers, finite floats, null, lists, and string-keyed objects. Unsupported
Python values are rejected rather than stringified.

Gateway-owned evidence fields (`attempt_id`, `grant_id`, `permit_id`, `effect`)
cannot be overwritten by caller evidence.

### 10.3 Durability and exact-fact retry

Append is file-fsync backed. Newly created directory entries are fsynced where
the platform exposes that primitive; Windows relies on the writable file handle
because Python has no portable directory `FlushFileBuffers` equivalent.

A write can become visible before fsync reports success. Therefore an exact
retry of the **same durable fact**—same effect, state, immutable lineage, and
evidence; timestamp ignored—re-fsyncs the existing fact rather than appending a
duplicate transition. Changed evidence is not treated as the same fact.

### 10.4 Writer serialization

All `EffectLedger` instances targeting the same normalized path share one
process-local re-entrant lock. History validation + append is one critical
section inside the supported single-process runtime. Independent external
process writers are out of scope.

### 10.5 Failure posture

Malformed JSON, corrupt record schema, impossible state transitions, changed
lineage, or a corrupt tail fail closed. Losing a safety fact is worse than
refusing further mutation.

---

## 11. RecoveryGuard (layer 6)

```text
EffectLedger
  → derive unresolved RESERVED / EFFECT_UNKNOWN semantic keys
  → hydrate RecoveryGuard
  → deny matching mutation before browser interaction
```

The deny result is `reconciliation_required`; health reporting is diagnostic,
not enforcement.

A missing BEST_EFFORT record is not evidence of no effect. Such replay is
permitted only under proven replay semantics.

`EFFECT_UNKNOWN` is intentionally terminal in layer 3. Future reconciliation
must add explicit evidence-bearing resolution semantics rather than silently
overwriting historical unknown state.

---

## 12. Invocation journal versus EffectLedger

```text
.webwire/journal.ndjson
    audit / diagnostics / legacy pre-M5 live-runtime hydration
    best-effort I/O

.webwire/effects.ndjson
    M5 safety facts
    fsync-backed
    corruption / REQUIRED reservation failure is fail-closed
```

M5 invariant:

> The EffectLedger is authoritative for every effect fact M5 durably records:
> REQUIRED reservations and terminal successors, plus surviving BEST_EFFORT
> confirmed/unknown terminal facts. A missing BEST_EFFORT fact after crash is
> not evidence of no effect. The invocation journal is not M5 commit authority.

During staged migration, the existing live write path may continue journal-
hydrated dedupe/budget behavior. Layer 7 retires that legacy safety role only
after concrete capabilities and RecoveryGuard have moved to M5.

---

## 13. Frozen invariants

1. Completed M5 remote mutations cross one Commit Gateway.
2. Execution authority is scoped to the approved semantic effect.
3. Approval and execution attempts are separate records.
4. Clean proven precommit failure can preserve human approval.
5. Only one attempt owns an approval claim at a time.
6. One attempt owns one stable effect identity.
7. One immutable intent snapshot owns one commit sequence's authority lineage,
   including a non-empty persistable target binding.
8. REQUIRED reservation start is latched before durable I/O.
9. Once REQUIRED reservation I/O begins, generic clean release is forbidden.
10. No REQUIRED mutation authority is minted unless durable reservation succeeds.
11. Approval/epoch/kill validity is checked again at the final mint boundary;
    direct epoch changes are fenced with authority creation.
12. A now-invalid fenced pre-permit reservation is closed `NO_EFFECT`, or remains
    unresolved if closure cannot be persisted.
13. Grant and permit TTLs are process-local elapsed-time authority: production
    defaults are monotonic, permit TTL begins at actual mint, permit expiry is
    sampled again at actual consume, and grant/permit clock values are never
    compared across domains.
14. Durable unresolved reservation/unknown state blocks automatic semantic
    replay once RecoveryGuard is integrated.
15. Explicit unknown outcomes are never blindly retried.
16. BEST_EFFORT without a durable fact is replayable only under proven replay
    safety.
17. Risk, authority, replay semantics, and durability are independent policy
    dimensions.
18. In-process kill activation invalidates old authority with strict
    process-local ordering; external hot-file kills are re-observed at final
    authority boundaries and make no unsupported cross-process atomicity claim.
19. Ledger history is monotonic and lineage-immutable; contradictory history
    fails closed.
20. Verification and reconciliation report evidence rather than inferred
    success.

---

## 14. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| T1 | REQUIRED reservation append/fsync reports failure | No permit; grant not spent; same claim/attempt/effect identity retained; reservation-start latch prevents generic clean release; exact-fact durability retry allowed |
| T2 | Crash after REQUIRED reservation before external mutation | Restart projects unresolved unknown; no automatic submit |
| T3 | Crash after REQUIRED submit before terminal append | Restart sees unresolved reservation; no duplicate submit |
| T4 | External mutation times out and runtime survives | Durable `EFFECT_UNKNOWN`; automatic replay denied |
| T5 | Verification fails after confirmed external effect | Confirmed effect remains durable; verification failure does not authorize replay |
| T6 | Capability attempts effect outside its authority | Scoped layer rejects before browser interaction |
| T7 | Permit for target A used on B | Gateway rejects |
| T8 | Permit reuse | Rejected |
| T9 | Permit expires unused | Rejected; canonical attempt closes `NO_EFFECT`; REQUIRED fence closes durably first; approval remains `SPENT` |
| T10 | Actor changes after approval | Rejected |
| T11 | Intent payload/media changes after approval | Rejected by intent binding |
| T12 | Audit journal write fails after M5 effect fact | EffectLedger safety semantics unchanged |
| T13 | Clean precommit failure before reservation start | Claim released; grant may remain `ACTIVE`; bounded retry budget advances |
| T14 | REQUIRED reservation durably established | Post-reservation crash cannot reclaim ephemeral approval; unresolved effect recoverable |
| T15 | Caller mutates `WriteIntent` while REQUIRED fsync blocks | Reservation and permit remain bound to one validated immutable snapshot |
| T16 | REQUIRED reservation/pruning consumes more than permit TTL | Returned permit still receives full TTL from actual mint time |
| T17 | Approval expires or epoch changes after REQUIRED reservation but before permit mint | No permit; durable `RESERVED -> NO_EFFECT`; attempt terminalized without approval reuse |
| T18 | T17 close append fails | No permit; raw `RESERVED` remains unresolved/fail-closed |
| T19 | System wall clock moves while process-local authority is live | Grant/permit TTL enforcement is unaffected because production defaults use monotonic clocks; ledger timestamps remain UTC wall time |
| T20 | Direct authorization epoch bumps while final mint/consume is competing | Epoch fence gives one process-local ordering: stale authority is denied if bump wins; otherwise authority crossing wins first |
| T21 | Permit expires while consume waits behind policy/epoch work | Fresh boundary-time clock denies consumption and closes the unused permit `NO_EFFECT` |
| T22 | External hot file appears during REQUIRED reservation or blocked consume | Final callback-free re-observation denies mint/consume; fenced pre-permit reservation closes `NO_EFFECT` |
| T23 | BEST_EFFORT grant and intent both carry empty target type or id | Gateway denies `target_missing` before permit mint; grant remains unspent, attempt PREPARING, and no ledger row exists |

Additional mandatory regressions include:

- concurrent grant claims -> exactly one owner;
- concurrent permit consumers -> exactly one successful consumption;
- competing confirmed/unknown writers -> exactly one terminal fact;
- live policy registration cannot interleave inside final permit consumption;
- in-process kill activation and authority crossing have one process-local
  linearization;
- external hot-file creation during slow reservation/consume is re-observed at
  the final boundary and blocks authority;
- reset listener cannot suppress critical revocation;
- re-entrant kill listeners do not recursively duplicate delivery;
- reset+retrip creates distinct listener generations;
- callbacks execute outside kill-state lock;
- direct authorization-epoch bump cannot race past final mint/consume;
- fenced unused expiry persists `NO_EFFECT` before eviction;
- permit expiry while blocked on authority checks is re-evaluated at consume;
- outcome APIs require canonical attempt object;
- caller evidence cannot forge reserved correlation keys;
- corrupt tail / malformed schema / changed lineage / illegal transitions fail
  closed;
- same-path ledger writers are serialized process-locally;
- “bytes written, fsync failed” exact-fact retry re-establishes durability
  without duplicate rows;
- ambiguous reservation retry uses stable attempt-owned `effect_id`;
- clean release is denied after reservation I/O starts;
- terminal evidence is strict JSON and persistence failure stays retryable
  without restoring execution authority;
- grant/attempt public lifecycle fields reject direct mutation;
- `SAFE_*` policy assignments require concrete broker-level replay evidence;
- unimplemented or unproven mutation families remain `UNKNOWN` / `REQUIRED`;
- caller-owned intent mutation during blocked reservation cannot alter durable or
  permit lineage;
- missing target type/id is denied before either REQUIRED reservation or
  BEST_EFFORT permit mint;
- permit TTL begins after slow reservation work;
- grant expiry during reservation uses the grant clock, not the gateway clock;
- default grant/permit authority clocks are monotonic while ledger timestamps
  remain UTC wall-clock provenance;
- failed pre-permit cancellation leaves the reservation unresolved and mints no
  authority.

### Remaining evidence boundaries

- Windows durability behavior is code/test-modeled but not yet verified by a
  Windows CI runner.
- External hot-file kill activation is final-boundary re-observed but is not a
  strict cross-process atomic transition; a stronger guarantee requires a
  cooperating cross-process protocol.
- live browser behavior belongs to layer-5 capability migration.
- end-to-end restart denial belongs to layer-6 RecoveryGuard integration.
- future reconciliation needs its own evidence-bearing terminal resolution
  design.

---

## 15. Build order

```text
1. EffectPolicy + EffectLedger primitives                  DONE
2. ApprovalGrant / EffectAttempt models                    DONE
3. Commit Gateway                                          CANDIDATE — independently reviewed / green
4. Scoped authorities                                      NEXT
5. Capability migration through the gateway
6. RecoveryGuard startup hydration + enforcement
7. Journal becomes audit-only in the live runtime
```

Later layers may extend the state model when new evidence requirements appear,
but they must not silently weaken earlier guarantees. Implementation/fault
proof may refine the design; preference alone does not reopen it.
