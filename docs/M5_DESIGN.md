# M5 — Effect Transaction Boundary (Design Specification)

```text
Status:   FROZEN FOR IMPLEMENTATION — REVISED BY IMPLEMENTATION EVIDENCE
Baseline: 7d081b9
Revision: layer-3 maintainer close-out, 2026-09-23
Change rule: revise only when implementation, fault injection, live evidence,
or an independently verified review finding falsifies an invariant or assumption.
```

This document is normative and self-contained. Build from this file, not from
review-conversation memory. `STATE.md` remains the living project record; during
staged migration it may still describe legacy live-runtime behavior that M5 has
not yet replaced.

The layer-3 implementation exercised the freeze rule repeatedly. The direction
of M5 did not change, but several details became more precise:

- BEST_EFFORT can crash after mutation and before a terminal ledger append;
- approval claiming must be a real synchronized compare-and-set;
- issued-but-unused authority can expire after approval was spent;
- a durable append can write bytes and still report failure during fsync;
- one execution attempt therefore needs one stable effect identity;
- once REQUIRED reservation I/O starts, failure is no longer equivalent to a
  clean precommit failure;
- the safety ledger must validate history, not merely record syntax;
- same-process safety requires serialization of validation + append;
- durable evidence must not be silently coerced during serialization.

---

## 1. Problem statement

WireAgent's pre-M5 write path has two verified architectural holes and one
conceptual conflation.

### Hole 1 — approved intent and execution authority are different scopes

A human can approve one semantic action while the capability receives the full
`WriteBroker`, whose surface contains unrelated mutations. Built-in
capabilities behave correctly, but the architecture does not enforce least
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
- a single process-local `CommitGateway`;
- single-use `EffectPermit` authority;
- fsync-backed `EffectLedger` safety facts;
- scoped broker authorities in layer 4;
- live capability migration in layer 5;
- restart enforcement through `RecoveryGuard` in layer 6.

---

## 2. What M5 does not guarantee

These negative guarantees are part of the contract.

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

---

## 3. Terminology

| Term | Meaning |
|---|---|
| **WriteIntent** | Declarative action, target, payload, risk metadata, semantic variant, and actor. |
| **ApprovalGrant** | Ephemeral record of one human approval, bound to intent, actor, target, policy identity, and authorization epoch. |
| **EffectAttempt** | One execution try against a grant. Owns stable `attempt_id`, stable `effect_id`, preparation/outcome state, and reservation-start latch. |
| **EffectPermit** | Single-use process-local execution authority descended from one grant/attempt lineage. |
| **CommitGateway** | The process-local authority boundary that validates current state and mints/consumes permits. |
| **EffectLedger** | Fsync-backed append-only M5 safety ledger at `.webwire/effects.ndjson`. |
| **EffectPolicy** | Registry declaration of risk, semantic effect scope, replay semantics, and derived durability. |
| **Authorization epoch** | Monotonic process-local revocation generation bound into grants and permits. |
| **Fenced effect** | `DurabilityPolicy.REQUIRED`; durable `RESERVED` must precede mutation authority. |
| **Non-fenced effect** | `BEST_EFFORT`; no precommit reservation, allowed only for proven replay-safe effects. |

---

## 4. EffectPolicy — independent axes

Every mutation action has one registry-controlled policy:

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
| like / unlike | `SAFE_STATE_SET` | `BEST_EFFORT` |
| bookmark / remove_bookmark | `SAFE_STATE_SET` | `BEST_EFFORT` |
| follow / unfollow | `UNKNOWN` | `REQUIRED` until implemented and proven |
| repost / unrepost | `UNKNOWN` | `REQUIRED` until implemented and proven |

The bookmark classification depends on the directional broker contract: already
bookmarked must be an already-satisfied no-op, never a fallback click on
`removeBookmark`.

---

## 5. ApprovalGrant and EffectAttempt

Approval and execution are separate state machines.

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

### 5.2 EffectAttempt

Canonical knowledge state:

```text
PREPARING
   ├──→ NO_EFFECT              # proven clean precommit failure
   └──→ RESERVED               # REQUIRED fence durably established
            ├──→ NO_EFFECT     # issued fenced permit expired unused
            ├──→ EFFECT_CONFIRMED
            └──→ EFFECT_UNKNOWN
```

For BEST_EFFORT, an issued permit can terminalize `PREPARING → NO_EFFECT` on
unused expiry or `PREPARING → EFFECT_*` after permit consumption.

Each attempt additionally owns two orthogonal, immutable/monotonic identities:

```text
attempt.effect_id            # stable for the lifetime of the attempt
attempt.reservation_started  # False → True only; REQUIRED path only
```

`effect_id` belongs to the attempt, **not** to each `authorize_commit()` call.
If a reservation append writes bytes and later reports fsync failure, retrying
that same attempt must address the same durable fact.

`reservation_started` is latched **before** calling the ledger. Once true, the
attempt is no longer eligible for generic clean-precommit `mark_no_effect()`:
the runtime cannot assume no durable reservation exists merely because the
append call raised.

### 5.3 Three distinct no-effect situations

1. **Clean precommit:** no reservation I/O began and no mutation authority was
   granted. Claim may be released; grant can remain `ACTIVE` within retry budget.
2. **Unused issued authority:** permit existed but expired unconsumed. Approval
   is already `SPENT`; attempt closes `NO_EFFECT` without restoring approval.
3. **Ambiguous reservation failure before permit mint:** no external mutation
   authority was minted, but reservation bytes may exist. The claim stays held,
   the same attempt/effect identity is retained, and generic clean release is
   forbidden. Retry/reconciliation must preserve that lineage.

`COMMIT_ATTEMPTED` / `submit_call_started` / `submit_call_returned` may be
useful diagnostics but are not canonical safety states.

---

## 6. Unified spend rule

> An ApprovalGrant is reusable only after clean, proven precommit `NO_EFFECT`.
> It becomes irrevocably spent when the gateway grants authority capable of the
> approved external effect. REQUIRED authority is exposed only after the durable
> reservation exists; BEST_EFFORT authority is spent at process-local permit
> mint.

### 6.1 REQUIRED protocol

The live process holds, in order:

```text
gateway protocol lock
  → kill execution fence
    → policy registry fence
      → grant claim fence
```

Then:

```text
validate current bindings
→ latch reservation_started
→ append exact RESERVED fact
→ fsync ledger (and new directory entry where the platform exposes it)
→ mark attempt RESERVED
→ mark grant SPENT
→ mint one permit
```

No process-local transaction can make disk and Python state atomic. Durable
`RESERVED` therefore dominates ephemeral grant state after a crash.

If the durable append reports failure:

- no permit is minted;
- grant is not spent;
- claim remains held by the same attempt;
- `reservation_started` remains true;
- the attempt's stable `effect_id` is retained;
- a retry of the exact same reservation may re-establish durability without
  appending a duplicate fact;
- generic clean-precommit release is forbidden.

### 6.2 BEST_EFFORT protocol

For proven replay-safe effects:

```text
validate current bindings
→ grant SPENT
→ mint one permit
```

There is no precommit durable reservation. If the process survives, known
confirmed/unknown outcomes are still appended durably. If it crashes between
remote mutation and terminal append, replay safety—not a fictitious missing
record—provides the guarantee.

### 6.3 Spend is not reversed by permit expiry

Permit expiry proves only that the particular permit never crossed the mutation
boundary. Fenced expiry first persists `RESERVED → NO_EFFECT`; then the canonical
attempt terminalizes and permit lineage is evicted. The grant remains `SPENT`.

---

## 7. Authorization epoch and kill linearization

Kill activation must invalidate outstanding execution authority even after an
operator resets the visible kill state.

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

Listener callbacks execute **outside** the kill-state lock. The lock protects
state/generation selection and execution-fence linearization, but arbitrary
listener code cannot invert the gateway-lock/kill-lock order. Re-entrant
listeners are tracked per listener/generation; a reset+retrip cannot cause an
old callback to satisfy a newer generation.

External hot-file creation cannot share the Python lock; it retains
check-at-observation semantics. Once observed, the same generation and critical
revocation rules apply.

---

## 8. Commit Gateway protocol

```text
Human-approved intent
        │
        ▼
ApprovalGrant ACTIVE
        │
        ▼
EffectAttempt claims grant
        │
        ▼
prepare without remote mutation
        │
   ┌────┴───────────┐
   │                │
clean failure      ready
   │                │
NO_EFFECT       CommitGateway
release claim       │
                 policy
                 /    \
         BEST_EFFORT  REQUIRED
             │           │
          spend      latch reservation-start
          permit     durable RESERVED + fsync
             │           │
             │        spend + permit
             └──────┬────┘
                    ▼
             single-use permit
                    │
            ┌───────┴────────┐
            │                │
       expires unused     consumed
            │                │
       NO_EFFECT        external effect
                            │
                     ┌──────┴──────┐
                     ▼             ▼
              EFFECT_CONFIRMED  EFFECT_UNKNOWN
                                      │
                              reconciliation required
```

Gateway invariants:

1. In the completed M5 path, every remote mutation crosses the gateway.
2. Claim ownership is synchronized and held through authority creation.
3. One attempt has one stable `effect_id`.
4. REQUIRED reservation I/O is latched before the ledger call.
5. No REQUIRED mutation authority is minted until reservation durability
   succeeds.
6. A reservation failure cannot be converted into generic clean precommit
   release.
7. Permit validation/consumption is atomic under the gateway protocol lock.
8. Kill state/pending critical revocation, epoch, policy identity, actor,
   target, intent, and effect scope are checked at the authority boundary.
9. The gateway accepts only the exact permit object it issued and retains the
   exact canonical attempt object for terminal lifecycle mutation.
10. Durable outcome append precedes in-memory terminalization/eviction.

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

Durable record identity fields are genuine non-empty strings; optional lineage
fields are either `None` or non-empty strings. Evidence is strict portable JSON:
strings, booleans, integers, finite floats, null, lists, and string-keyed objects.
Unsupported Python values are rejected; they are never silently stringified.

Gateway-owned evidence fields (`attempt_id`, `grant_id`, `permit_id`, `effect`)
cannot be overwritten by caller evidence.

### 10.3 Durability and exact-fact retry

Append is file-fsync backed. Newly created directory entries are fsynced where
the platform exposes that primitive; Windows deliberately relies on the file
handle because Python has no portable directory `FlushFileBuffers` equivalent.

A write can become visible before fsync reports success. Therefore an exact
retry of the **same durable fact**—same effect, state, immutable lineage, and
evidence; timestamp ignored—does not append a duplicate transition. It re-fsyncs
the existing fact and parent directory where supported. Changed evidence is not
silently treated as the same fact.

### 10.4 Writer serialization

All `EffectLedger` instances targeting the same normalized path share one
process-local re-entrant lock. History validation + append is one critical
section inside the supported single-process runtime.

Independent external processes writing the same NDJSON file are not coordinated
by this mechanism and are out of scope.

### 10.5 Failure posture

Malformed JSON, corrupt record schema, impossible state transitions, or changed
lineage fail closed. A partial/corrupt tail is not skipped. Operator repair may
be required; losing a safety fact is worse than refusing further mutation.

---

## 11. RecoveryGuard (layer 6)

Startup behavior:

```text
EffectLedger
  → derive unresolved RESERVED / EFFECT_UNKNOWN semantic keys
  → hydrate RecoveryGuard
  → deny matching mutation before browser interaction
```

The deny result is `reconciliation_required`; health reporting is diagnostic,
not the enforcement mechanism.

A missing BEST_EFFORT record is not evidence of no effect. Such replay is
permitted only under the action's proven replay semantics.

`EFFECT_UNKNOWN` is intentionally terminal in layer 3. Future reconciliation
must add explicit evidence-bearing resolution semantics (for example,
reconciled-present / reconciled-absent) rather than silently overwriting
historical unknown state.

---

## 12. Invocation journal versus EffectLedger

Two files have different contracts:

```text
.webwire/journal.ndjson
    audit / diagnostics / legacy pre-M5 live-runtime hydration
    best-effort I/O

.webwire/effects.ndjson
    M5 safety facts
    fsync-backed
    corruption / required reservation failure is fail-closed
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
7. REQUIRED reservation start is latched before durable I/O.
8. Once REQUIRED reservation I/O begins, generic clean release is forbidden.
9. No REQUIRED mutation authority is minted unless durable reservation succeeds.
10. Durable unresolved reservation/unknown state blocks automatic semantic
    replay once RecoveryGuard is integrated.
11. Explicit unknown outcomes are never blindly retried.
12. BEST_EFFORT without a durable fact is replayable only under proven replay
    safety.
13. Risk, authority, replay semantics, and durability are independent policy
    dimensions.
14. Kill activation invalidates old authority; pending critical revocation is
    fail-closed.
15. Ledger history is monotonic and lineage-immutable; contradictory history
    fails closed.
16. Verification and reconciliation report evidence rather than inferred
    success.

---

## 14. Acceptance tests

The original T1–T14 contract remains, with T1 refined by implementation
evidence.

| # | Scenario | Required outcome |
|---|---|---|
| T1 | REQUIRED reservation append/fsync reports failure | No permit; grant not spent; same claim/attempt/effect identity retained; reservation-start latch prevents generic clean release; exact-fact durability retry is allowed |
| T2 | Crash after REQUIRED reservation before external mutation | Restart projects unresolved unknown; no automatic submit |
| T3 | Crash after REQUIRED submit before terminal append | Restart sees unresolved reservation; no duplicate submit |
| T4 | External mutation times out and runtime survives | Durable `EFFECT_UNKNOWN`; automatic replay denied |
| T5 | Verification fails after confirmed external effect | Confirmed effect remains durable; verification failure does not authorize replay |
| T6 | Capability attempts semantic effect outside its authority | Scoped layer rejects before browser interaction |
| T7 | Permit for target A used on B | Gateway rejects |
| T8 | Permit reuse | Rejected |
| T9 | Permit expires unused | Rejected; canonical attempt closes post-authority `NO_EFFECT`; REQUIRED fence closes durably first; approval remains `SPENT` |
| T10 | Actor changes after approval | Rejected |
| T11 | Intent payload/media changes after approval | Rejected by intent binding |
| T12 | Audit journal write fails after M5 effect fact | EffectLedger safety semantics unchanged |
| T13 | Clean precommit failure before reservation start | Claim released; grant may remain `ACTIVE`; bounded retry budget advances |
| T14 | REQUIRED reservation durably established | Post-reservation crash cannot reclaim the same ephemeral approval; unresolved effect is recoverable |

Additional mandatory layer-3 regressions:

- concurrent grant claims produce exactly one owner;
- concurrent permit consumers produce exactly one successful consumption;
- competing confirmed/unknown terminal writers cannot both commit;
- live policy registration cannot interleave between final policy validation and
  permit consumption;
- kill activation and authority crossing have one process-local linearization
  order;
- a reset listener cannot suppress the gateway's critical revocation event;
- kill listeners may re-enter observation/trip/registration without recursive
  duplicate delivery;
- reset+retrip creates distinct listener generations;
- callbacks execute outside the kill-state lock;
- fenced unused expiry persists `NO_EFFECT` before attempt/permit eviction;
- outcome APIs require the canonical attempt object;
- reserved terminal correlation keys cannot be forged by caller evidence;
- corrupt tail / malformed schema / changed lineage / illegal transitions fail
  closed;
- same-path ledger writers are serialized process-locally;
- “bytes written, fsync failed” exact-fact retry re-establishes durability
  without duplicate rows;
- ambiguous reservation retry uses the attempt's stable `effect_id`;
- clean release is denied after reservation I/O starts;
- terminal evidence must be strict JSON and persistence failure remains
  retryable without restoring execution authority;
- grant/attempt public lifecycle fields reject direct mutation;
- unimplemented future mutation families remain `UNKNOWN` / `REQUIRED`.

### Remaining evidence boundaries

- Windows durability behavior is code/test-modeled but not yet verified by a
  Windows CI runner.
- live browser behavior belongs to layer-5 capability migration.
- end-to-end restart denial belongs to layer-6 RecoveryGuard integration.
- future reconciliation needs its own evidence-bearing terminal resolution
  design.

---

## 15. Build order

```text
1. EffectPolicy + EffectLedger primitives                  DONE
2. ApprovalGrant / EffectAttempt models                    DONE
3. Commit Gateway                                          CANDIDATE — reviewed/green
4. Scoped authorities                                      NEXT
5. Capability migration through the gateway
6. RecoveryGuard startup hydration + enforcement
7. Journal becomes audit-only in the live runtime
```

Later layers may extend the state model when new evidence requirements appear,
but they must not weaken earlier guarantees silently. The governing rule remains:
implementation/fault evidence may refine the design; preference alone does not
reopen it.