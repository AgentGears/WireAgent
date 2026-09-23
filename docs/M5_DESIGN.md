# M5 — Effect Transaction Boundary (Design Specification)

```text
Status:   FROZEN FOR IMPLEMENTATION — REVISED BY IMPLEMENTATION EVIDENCE
Baseline: 7d081b9
Revision: layer-3 first-pass findings on 2026-09-23
Change rule: revise only when implementation, fault injection, or live
evidence falsifies an invariant or assumption. Design rounds are over;
the next useful disagreement is a failing test.
```

This document is normative and self-contained. An implementer with no
access to the review conversation that produced it builds from this file
alone. Review provenance lives in `STATE.md`.

The 2026-09-23 layer-3 first pass exercised the freeze rule rather than
reopening design by preference. It established three facts the original
wording did not model precisely enough: a BEST_EFFORT effect can crash
after the remote mutation but before a terminal ledger append; an issued
but unconsumed permit can expire after approval authority has already
been spent; and the approval claim called a CAS must be an actual
synchronized compare-and-set. The revisions below make those facts
explicit without changing M5's architectural direction.

---

## 1. Problem statement

WireAgent's write path has two verified correctness holes and one
conceptual conflation.

**Hole 1 — execution authority is not scoped to the approved intent.**
A human approves one semantic action (for example, `like post 123`), but
after confirmation the kernel hands the capability the complete
`WriteBroker`, whose surface contains every mutation method: like,
unlike, bookmark, post, reply, quote, media upload, delete. Built-in
capabilities behave correctly, so nothing is exploited today. The
architecture does not enforce least authority at runtime. `ports.py`
documents per-port scoping that no code implements.

**Hole 2 — replay-unsafe external effects can escape durable safety
state.** The kernel's `"journalled"` trace stage is in-memory. The durable
journal append happens later, in the dispatcher, after the kernel returns
— and the journal deliberately swallows I/O errors. Between an
irreversible external submit and its durable record sits the entire
verification tail. A crash in that window can leave a live public effect
with no durable safety record; restart hydration then has nothing to
block a duplicate.

**Conflation — one risk axis answering three questions.**
`RiskTier` currently governs approval strictness, execution authority,
and what happens after uncertainty. The code demonstrates why that is
wrong: `post` and `delete_post` share the highest tier and have different
replay behavior, while low-risk state-setting actions can be safe to
repeat only when the implementation actually proves that property.
Risk, authority, and replay safety are independent dimensions.

M5 closes both holes and separates the dimensions with one architecture:
a **Commit Gateway** every remote mutation must cross, backed by a durable
**EffectLedger**, authorized by single-use **EffectPermits** derived from
human approval, with restart recovery that blocks unresolved durable
facts rather than merely warning.

## 2. What M5 does not guarantee

These negative guarantees are part of the contract. The implementation
must not quietly grow stronger claims than the mechanism supports.

- **No exactly-once guarantee across WireAgent and external systems.**
  There is no shared transaction manager. For `REQUIRED` effects the
  target is at-most-once *automatic* execution plus explicit
  reconciliation for durable uncertainty. A `BEST_EFFORT` action may be
  executed again after a crash that left no terminal ledger fact only
  because its registry-controlled replay semantics have been proven safe:
  re-execution cannot create an additional meaningful external effect.
- **No hard security isolation for hostile same-process extensions.**
  Scoped authorities prevent accidental misuse, misregistration, and
  authority creep. Deliberately malicious Python in the same process can
  still reach objects it can otherwise obtain. Process isolation is a
  future requirement for untrusted adapters, not an M5 property.
- **No automatic reconciliation in this milestone.** The reconciliation
  *gate* (deny until resolved) is in scope; an automated `reconcile()`
  capability is not.
- **No persistence of ApprovalGrants across restart.** This is deliberate:
  approval grants are ephemeral; durable safety state belongs to the
  EffectLedger.
- **No claim that absence of a BEST_EFFORT ledger record proves no effect.**
  BEST_EFFORT deliberately has no pre-mutation durable reservation. Its
  crash safety comes from test-backed replay semantics, not from a
  fictitious durable fact.
- **No assumption that DOM selector behavior constitutes replay safety.**
  Replay semantics are code-owned, registry-declared, and regression-
  backed against the real broker implementation.

## 3. Terminology

| Term | Meaning |
|---|---|
| **WriteIntent** | The existing declarative description a capability's `compose()` produces: action type, target, risk metadata, payload, actor. |
| **ApprovalGrant** | The record of one human approval. Ephemeral (in-memory). Carries intent identity, policy identity, and authorization epoch. |
| **EffectAttempt** | One execution attempt against an ApprovalGrant. Owns preparation and outcome state. |
| **EffectPermit** | Single-use process-local execution authority minted at the Commit Gateway, descended from an ApprovalGrant. |
| **Commit Gateway** | The single boundary every remote mutation crosses. Validates and consumes the permit; performs durable fencing when policy requires it. |
| **EffectLedger** | The fsync-backed safety ledger (`.webwire/effects.ndjson`). It is authoritative for facts that M5 durably records; it does not fabricate a fact for a BEST_EFFORT crash window. |
| **EffectPolicy** | The per-action declaration of risk, allowed effects, replay semantics, and durability. Registry-controlled. |
| **Authorization epoch** | A monotonically increasing counter. Grants and permits are bound to the epoch at which they were issued; a kill trip invalidates older authority. |
| **Fenced / non-fenced effect** | Whether the gateway must durably reserve before mutation (`REQUIRED`) or relies on process-local authority plus proven replay safety (`BEST_EFFORT`). |

## 4. EffectPolicy — four independent axes

Every registered action declares an `EffectPolicy`:

```text
EffectPolicy
├── risk                 # impact dimension
│     approval strength, warnings, budgets
├── allowed_effects      # authority dimension
│     exact semantic effect verbs
├── replay_semantics     # delivery/replay dimension
└── durability           # REQUIRED | BEST_EFFORT
      whether the gateway must durably fence before mutation
```

### 4.1 ReplaySemantics

Replay semantics — not merely "idempotency" — because equivalent final
state is insufficient if replay re-sends a notification, re-amplifies a
target, re-fires a callback, or re-bills.

```text
SAFE_STATE_SET              # replay cannot create an additional meaningful
                            # external effect (directional state-setting)
SAFE_TARGET_DELETE          # target-scoped removal; replay finds target absent
NON_IDEMPOTENT_CREATE       # replay can create another independent object
REPLAY_HAS_RESIDUAL_EFFECTS # same final state but repeated external signals
UNKNOWN                     # not established by implementation evidence
```

**Rules:**

- `UNKNOWN` is conservatively fenced (`REQUIRED`) until proven otherwise.
- Replay semantics are **registry-controlled and test-backed**. A
  capability author cannot gain weaker durability by asserting safety;
  `SAFE_*` requires a regression against the real semantic broker
  implementation.
- Unimplemented/future actions remain `UNKNOWN`. A planned directional
  API is not evidence.
- `BEST_EFFORT` is a positive replay-safety claim, not merely permission
  to omit fsync. It means a crash after remote mutation but before a
  terminal ledger append can be followed by replay without producing an
  additional meaningful external effect.
- An explicitly recorded `EFFECT_UNKNOWN` is still unresolved and is not
  automatically retried. The BEST_EFFORT exception concerns only a crash
  that leaves **no durable effect fact**.
- Durability derives from **risk and replay semantics together**. A
  consequential action can remain `REQUIRED` even when replay-safe; a
  low-risk replay-unsafe action is also `REQUIRED`.

### 4.2 Initial registry assignments

| Action | replay_semantics | durability |
|---|---|---|
| post / reply / quote (+ media variants) | `NON_IDEMPOTENT_CREATE` | `REQUIRED` |
| delete_post | `SAFE_TARGET_DELETE` | `REQUIRED` (consequential) |
| like / unlike | `SAFE_STATE_SET` | `BEST_EFFORT` (directional broker + regression-backed) |
| bookmark / remove_bookmark | `SAFE_STATE_SET` | `BEST_EFFORT` (directional broker + regression-backed) |
| follow / unfollow (future) | `UNKNOWN` | `REQUIRED` until real implementation evidence exists |
| repost / unrepost (future) | `UNKNOWN` | `REQUIRED` until real implementation evidence exists |

## 5. ApprovalGrant and EffectAttempt — two state machines

Approval and execution are distinct records. Approval state never moves
backward; execution attempts are disposable but must end in a state that
matches what the gateway knows.

### 5.1 ApprovalGrant

```text
state:      ACTIVE → SPENT | EXPIRED | REVOKED
claim:      claimed_by: Optional[attempt_id]   (orthogonal synchronized CAS)
bindings:   intent_hash, actor_id, action_type, target,
            policy_binding, authorization_epoch
validity:   expires_at; max_precommit_attempts
```

- `SPENT` is terminal and irrevocable.
- `EXPIRED` is time-driven.
- `REVOKED` is caused by epoch mismatch or operator action.
- `claim()` is an actual synchronized compare-and-set: at most one live
  attempt owns a grant at a time.
- The Commit Gateway holds the grant's **claim fence** across the entire
  authority-granting critical section. A concurrent clean-failure path
  cannot release the claim between validation, reservation, spend, and
  permit minting.
- A clean precommit `NO_EFFECT` releases the claim and leaves an `ACTIVE`
  grant reusable within its attempt budget.
- Once authority has been spent, the claim can never be released back
  into reusable approval.
- Grants are ephemeral by design; durable safety state belongs to the
  EffectLedger.

### 5.2 EffectAttempt

```text
PREPARING
   ├──→ NO_EFFECT              # clean precommit, or unused non-fenced permit expiry
   └──→ RESERVED               # durable fence established (REQUIRED only)
            ├──→ NO_EFFECT     # issued permit expired unconsumed
            ├──→ EFFECT_CONFIRMED
            └──→ EFFECT_UNKNOWN
```

There are two semantically different `NO_EFFECT` paths:

1. **Clean precommit `NO_EFFECT`:** no authority capable of producing the
   effect was granted. The attempt releases the claim; approval remains
   `ACTIVE` and the precommit budget advances.
2. **Post-authority `NO_EFFECT`:** a permit was issued but expired
   unconsumed. The effect is proven not to have crossed the mutation
   boundary, but the human approval was already `SPENT`. The attempt is
   terminalized without releasing the claim or restoring approval.

For non-fenced effects, `RESERVED` is absent. An unconsumed permit can
therefore terminalize `PREPARING → NO_EFFECT` on expiry after the grant
has already become `SPENT`.

`COMMIT_ATTEMPTED` / `submit_call_started` / `submit_call_returned` may
be diagnostic events, never canonical safety state. A local process
cannot atomically prove an external call across a crash; canonical
states describe what WireAgent knows.

### 5.3 Approval validity is not a retry budget

A clean precommit failure does not consume approval, but it does not
license an unbounded automated loop:

```text
ApprovalGrant.expires_at                # wall-clock validity
ExecutionPolicy.max_precommit_attempts  # e.g. 3
```

After `max_precommit_attempts` clean failures, automated execution stops
even though the grant may still be `ACTIVE`; a human starts over or
approves fresh.

## 6. The unified spend rule

> **An ApprovalGrant remains reusable only after a clean, proven
> precommit `NO_EFFECT`. It becomes irrevocably spent when the Commit
> Gateway grants authority capable of producing the approved external
> effect. For `REQUIRED` effects, that point follows successful durable
> `EFFECT_RESERVED`; for `BEST_EFFORT` effects, it is the process-local
> transition that mints the single-use effect permit.**

Permit expiry does not reverse spend. It closes the corresponding
attempt as post-authority `NO_EFFECT` while the ApprovalGrant remains
`SPENT`.

### 6.1 Fenced atomicity is protocol-level, not storage-level

No transaction spans the filesystem ledger and an in-memory grant
object. For `REQUIRED` effects:

> **Once `EFFECT_RESERVED` has durably committed, the approval is
> irrevocably considered spent, irrespective of whether its transient
> object was subsequently updated before a crash.**

Within a live process, the gateway holds the grant's synchronized claim
fence across:

```text
validate → durable append → fsync → mark RESERVED → mark SPENT → mint permit
```

The claim cannot be concurrently released inside that sequence. After a
crash, the ephemeral grant disappears and RecoveryGuard reconstructs the
unresolved fence from the ledger.

### 6.2 Why reservation, not the browser click

Burning approval later at the external call would permit a
post-reservation crash to be followed by reuse of the same approval.
Spending at reservation makes the durable fence self-defending. Burning
at the initial phase-two claim would instead waste human approval on
clean preparation failures that provably produced no effect.

## 7. Authorization epoch

The kill switch extends from "deny while active" to "permanently revoke
outstanding execution authority":

```text
authorization_epoch: 42
ApprovalGrant.epoch = 42
EffectPermit.epoch = 42

trip generation → authorization_epoch = 43
older grants/permits → invalid
```

Trip notification is generation-based. The gateway's epoch listener is
**critical**: once a trip generation is observed, authority remains
fail-closed until that critical revocation event has been delivered,
even if an earlier listener resets the visible kill flag. Listener
callbacks execute outside the kill-state lock to avoid cross-thread lock
inversion. The gateway drains listener obligations before taking its
protocol lock and then uses a kill execution fence to close the race
between preflight and authority crossing.

The same epoch mechanism can generalize to future operator-wide
authorization resets. Persisting epoch state, if required beyond the
lifetime of ephemeral grants, belongs to later integration/recovery
wiring; layer 3 defines and enforces the process-local comparison.

## 8. The Commit Gateway protocol

```text
Human-approved intent
        │
        ▼
  ApprovalGrant ACTIVE
        │
        ▼
  one EffectAttempt claims it     (synchronized CAS on claimed_by)
        │
        ▼
  prepare without remote mutation
        │
   ┌────┴─────────┐
   │              │
clean NO_EFFECT  ready
   │              │
release claim    ▼
grant ACTIVE   Commit Gateway
                  │
          ┌───────┴────────┐
          │                │
     BEST_EFFORT        REQUIRED
          │                │
     SPENT + permit   durable RESERVED + fsync
          │                │
          │            SPENT + permit
          └───────┬────────┘
                  ▼
          single-use EffectPermit
                  │
          ┌───────┴────────┐
          │                │
     expires unused    consumed
          │                │
     NO_EFFECT         external effect
   (grant stays SPENT)      │
                     ┌──────┴──────┐
                     ▼             ▼
             EFFECT_CONFIRMED  EFFECT_UNKNOWN
                                      │
                                      ▼
                           reconciliation_required
```

**Gateway invariants of operation:**

1. Every remote mutation eventually crosses this gateway; layer 3 builds
   the boundary before layer 5 migrates capabilities onto it.
2. The grant claim is synchronized and held through the authority-
   granting protocol.
3. The permit is validated and consumed atomically at the future
   mutation adapter boundary; it is single-use.
4. If a required durable reservation cannot be written, no authority
   capable of producing that effect is minted.
5. Kill state, pending critical revocation, epoch, policy identity,
   actor, target, intent, and effect scope are revalidated at the
   authority boundary.
6. The gateway retains the exact canonical `EffectAttempt` object for
   each issued permit. Correlation IDs cannot substitute a reconstructed
   object for terminal lifecycle mutation.

## 9. Scoped authorities

After the gateway mints a permit, layer 4 adapts the concrete broker to
a narrow authority surface matching `allowed_effects`:

```text
broker.authorize(permit) → LikeAuthority | PostAuthority | DeleteAuthority | ...
```

The full `WriteBroker` may implement many semantic methods; a capability
must receive only the authority surface for the approved effect. This is
a least-authority engineering boundary for trusted same-process code,
not a malicious-code sandbox.

## 10. Effect-knowledge states and the ledger

```text
NO_EFFECT          # proven: no external effect crossed the mutation boundary
RESERVED           # durable REQUIRED fence exists; terminal outcome pending
EFFECT_CONFIRMED   # external evidence establishes the effect
EFFECT_UNKNOWN     # may have happened; reconciliation required
```

- `REQUIRED` effects create `RESERVED` before remote mutation. After
  restart, a `RESERVED` without terminal outcome is projected as
  unresolved `EFFECT_UNKNOWN`; raw evidence is never rewritten.
- A `REQUIRED` permit that expires unconsumed appends durable `NO_EFFECT`
  before the process-local permit/attempt can be evicted or terminalized.
  If that append fails, closure remains retryable and the raw
  `RESERVED` continues to fail closed.
- `BEST_EFFORT` effects have **no durable precommit record by design**.
  If the process survives, known confirmed or explicitly unknown outcomes
  are durably appended. If the process crashes after the remote mutation
  and before that append, there may be no M5 effect record; safety then
  derives exclusively from the action's test-backed replay semantics.
- An explicitly durable `EFFECT_UNKNOWN`, fenced or non-fenced, remains
  unresolved and is never blindly retried.
- Diagnostic events may be recorded but carry no execution authority.

## 11. RecoveryGuard — enforcement, not advisory

On startup:

```text
EffectLedger → find durable unresolved reservations / unknown effects
            → RecoveryGuard hydrates their semantic keys
```

A matching new invocation receives, before browser mutation:

```text
DENY — reason: reconciliation_required — effect_id: <id>
```

This guarantee applies to **durable unresolved facts**. A BEST_EFFORT
crash can leave no fact to hydrate; such replay is permitted only because
`BEST_EFFORT` itself requires proven replay safety. Absence of a record
must never be described as proof that no BEST_EFFORT effect occurred.

Surfacing unresolved effects in `health` is diagnostic convenience only;
the deny path is the safety property.

## 12. Journal versus ledger

Two files, two contracts, two APIs:

```text
.webwire/journal.ndjson   — audit/diagnostic; best-effort
                            I/O failures do not define commit safety
.webwire/effects.ndjson   — M5 safety ledger; fsync-backed
                            REQUIRED reservation failure blocks authority
```

Invariant 15 is rewritten precisely:

> **The EffectLedger is authoritative for every effect fact M5 durably
> records: all REQUIRED reservations and their terminal successors, and
> BEST_EFFORT terminal/unknown facts that reached durable append. A
> missing BEST_EFFORT record after a crash is not evidence of no effect;
> replay is safe only under the registry's test-backed replay contract.
> The invocation journal is audit/diagnostic and is not M5 commit
> authority.**

During staged migration, the pre-M5 runtime may continue using journal-
hydrated dedupe/budgets until layers 5–7 move the live write path and
retire that legacy safety role.

## 13. The twelve frozen invariants

1. Every remote mutation in the completed M5 path crosses one Commit
   Gateway.
2. Every execution authority is scoped to the approved semantic effect.
3. Human approval and execution attempts are distinct records.
4. Clean, proven precommit failure does not consume human approval.
5. Only one execution attempt may claim an approval at once; the claim is
   a synchronized CAS and is fenced across commit authority creation.
6. For durability-required effects, successful durable reservation
   precedes and irrevocably implies approval spend before external
   mutation authority is exposed.
7. No authority capable of a REQUIRED external mutation is minted if the
   required durable reservation fails.
8. Any durable unresolved reservation or `EFFECT_UNKNOWN` blocks semantic
   replay until reconciliation.
9. An explicitly unknown or durably unresolved external outcome is never
   automatically retried. A crash with no BEST_EFFORT durable fact may be
   re-executed only when replay semantics are registry-controlled,
   implementation-tested, and safe.
10. Risk, authority, replay semantics, and durability are independent
    policy dimensions.
11. Kill-switch activation invalidates outstanding execution authority;
    a pending critical revocation event itself is fail-closed.
12. Verification and reconciliation report evidence, not inferred
    success.

## 14. Acceptance tests (T1–T14)

Fault-injection tests are the acceptance criteria; they land with the
layer they exercise, not after.

| # | Scenario | Required outcome |
|---|---|---|
| T1 | REQUIRED reservation fsync fails | Effect authority MUST NOT be minted; grant remains usable under its held claim |
| T2 | Crash after REQUIRED reservation, before commit | Restart → `reconciliation_required`; no automatic submit |
| T3 | Crash immediately after REQUIRED submit | Restart → unresolved reservation; no duplicate submit |
| T4 | External mutation returns timeout and runtime can record it | Durable `EFFECT_UNKNOWN`; automatic replay denied |
| T5 | Verification fails after observed effect | Effect remains recorded; no replay derived from verification failure |
| T6 | LikeCapability attempts `delete()` | Authority surface rejects before browser interaction |
| T7 | Permit for target A used on target B | Commit gateway rejects |
| T8 | Permit reuse | Rejected |
| T9 | Permit expires before commit | Rejected; canonical attempt → post-authority `NO_EFFECT`; approval remains `SPENT`; REQUIRED fence closes durably first |
| T10 | Actor changes between approval and commit | Rejected |
| T11 | Intent payload/media changes after approval | Rejected by intent binding |
| T12 | Audit journal write fails after successful effect recording | M5 safety ledger semantics are unchanged |
| T13 | Clean precommit failure preserves approval | Claim released; grant `ACTIVE`; same approval can fund another bounded attempt |
| T14 | REQUIRED reservation consumes approval | Post-reservation crash cannot reclaim same approval; durable unresolved key is recoverable |

Additional mandatory regressions from layer-3 implementation evidence:

- concurrent `ApprovalGrant.claim()` callers produce exactly one owner;
- a concurrent clean-failure release cannot interleave inside the
  gateway's claim-fenced reserve/spend/mint sequence;
- a reset listener registered before the gateway cannot suppress critical
  epoch revocation;
- terminal outcome APIs accept only the canonical attempt object retained
  by the gateway;
- unimplemented mutation families remain `UNKNOWN`/`REQUIRED` until real
  replay-safety evidence exists.

## 15. Build order

```text
1. EffectPolicy + EffectLedger primitives
2. ApprovalGrant / EffectAttempt models
3. Commit Gateway
4. Scoped authorities
5. Capability migration through the gateway
6. RecoveryGuard
7. Journal becomes audit-only in the live runtime
```

EffectPolicy precedes the gateway because the gateway's first questions
— what semantic authority is permitted, what replay guarantees are
proven, and whether durable fencing is required — are registry answers.
Adversarial tests land with the layer they exercise. A later layer must
not silently strengthen claims beyond what the earlier mechanism can
actually guarantee.
