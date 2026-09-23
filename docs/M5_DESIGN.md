# M5 — Effect Transaction Boundary (Design Specification)

```text
Status:   FROZEN FOR IMPLEMENTATION
Baseline: 7d081b9
Change rule: revise only when implementation, fault injection, or live
evidence falsifies an invariant or assumption. Design rounds are over;
the next useful disagreement is a failing test.
```

This document is normative and self-contained. An implementer with no
access to the review conversation that produced it builds from this file
alone. Review provenance lives in `STATE.md` (history entry m).

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

**Hole 2 — external effects can escape durable safety state.**
The kernel's `"journalled"` trace stage is in-memory. The durable journal
append happens later, in the dispatcher, after the kernel returns — and
the journal deliberately swallows I/O errors. Between an irreversible
external submit and its durable record sits the entire verification tail
(two fixed three-second sleeps plus unbounded-within-timeouts capture and
verification latency). A crash in that window leaves a live public post
with no durable record; restart hydration then has nothing to dedupe
against, and a retry can duplicate the effect.

**Conflation — one risk axis answering three questions.**
`RiskTier` currently governs approval strictness, execution authority,
and (via the M5 proposal it preceded) what happens after uncertainty.
The code demonstrates why that is wrong: `post` and `delete_post` share
the highest tier and have completely different duplicate-delivery
behavior, while `bookmark` — a low tier — was retry-safe only by
accident of undocumented DOM behavior. Risk, authority, and replay
safety are independent dimensions.

M5 closes both holes and separates the three dimensions with one
architecture: a **Commit Gateway** every remote mutation must cross,
backed by a durable **EffectLedger**, authorized by single-use
**EffectPermits** derived from human approval, with restart recovery
that blocks rather than warns.

## 2. What M5 does not guarantee

These negative guarantees are part of the contract. The implementation
must not quietly grow stronger claims than the mechanism supports —
the failure mode that originally produced invariant 15.

- **No exactly-once guarantee across WireAgent and external systems.**
  There is no shared transaction manager. The target property is
  at-most-once *automatic* execution plus explicit reconciliation for
  uncertain outcomes.
- **No hard security isolation for hostile same-process extensions.**
  Scoped authorities prevent accidental misuse, misregistration, and
  authority creep. Deliberately malicious Python in the same process can
  always reach the objects it is handed. Process isolation is a future
  requirement for untrusted adapters, not an M5 property.
- **No automatic reconciliation in this milestone.** The
  reconciliation *gate* (deny until resolved) is in scope; an automated
  `reconcile()` capability is not.
- **No persistence of ApprovalGrants across restart.** This is
  deliberate (§6): approval grants are ephemeral; durable safety state
  belongs to the EffectLedger.
- **No assumption that DOM selector behavior constitutes replay
  safety.** Replay semantics are a code-owned, registry-declared
  contract. The bookmark episode — retry-safe on the observed DOM only
  because both selectors happened to coexist and the click happened to
  be a no-op — is the standing reason this rule exists.

## 3. Terminology

| Term | Meaning |
|---|---|
| **WriteIntent** | The existing declarative description a capability's `compose()` produces: action type, target, risk metadata, payload, actor. Unchanged by M5. |
| **ApprovalGrant** | The record of one human approval. Ephemeral (in-memory). Carries intent identity, policy identity, and authorization epoch. |
| **EffectAttempt** | One execution attempt against an ApprovalGrant. Owns preparation and outcome state. |
| **EffectPermit** | Single-use execution authority minted at the Commit Gateway, descended from the ApprovalGrant. Scoped to the approved effect. |
| **Commit Gateway** | The single boundary every remote mutation crosses. Validates and consumes the permit; performs durable fencing when policy requires it. |
| **EffectLedger** | The fsync-backed, safety-critical record of effect state transitions (`.webwire/effects.ndjson`). Distinct from the audit journal. |
| **EffectPolicy** | The per-action declaration of risk, allowed effects, replay semantics, durability, and uncertainty policy. Registry-controlled. |
| **Authorization epoch** | A monotonically increasing counter. Grants are bound to the epoch at which they were issued; epoch bumps (kill switch) invalidate all outstanding grants. |
| **Fenced / non-fenced effect** | Whether the gateway must durably reserve before the mutation (fenced) or performs a process-local spend only (non-fenced). Determined by EffectPolicy. |

## 4. EffectPolicy — four independent axes

Every registered action declares an `EffectPolicy`:

```text
EffectPolicy
├── risk                 # impact dimension
│     approval strength, warnings, budgets
├── allowed_effects      # authority dimension
│     the exact effect verbs this action may perform
│     (e.g. like → {LIKE}; post → {OPEN_COMPOSER, FILL, ATTACH, SUBMIT})
├── replay_semantics     # delivery dimension
└── durability           # REQUIRED | BEST_EFFORT
      whether the gateway must durably fence this effect
```

### 4.1 ReplaySemantics

Replay semantics — not "idempotency" — because equivalent final state is
not sufficient. A replay that lands in the same state but re-sends a
notification, re-amplifies a target, re-fires a callback, or re-bills is
not safe to replay.

```text
SAFE_STATE_SET              # re-execution cannot create an additional
                            # meaningful external effect (a correct like)
SAFE_TARGET_DELETE          # target-scoped removal; replay finds the
                            # target absent (delete_post)
NON_IDEMPOTENT_CREATE       # replay can create another independent
                            # object (post, reply, quote)
REPLAY_HAS_RESIDUAL_EFFECTS # same final state but repeated external
                            # signals (notification, amplification,
                            # callback, billing)
UNKNOWN                     # not yet established
```

**Rules:**

- `UNKNOWN` is treated as `NON_IDEMPOTENT_CREATE` for durability
  purposes until proven otherwise.
- Replay semantics are **registry-controlled and test-backed**. A
  capability author cannot gain weaker durability by asserting
  idempotency; the declaration must be backed by a regression test
  demonstrating the property against the real broker implementation.
- Durability derives from **risk and replay semantics together**;
 neither replaces the other. A high-impact, replay-safe delete is still
 `REQUIRED` because it is consequential; a low-risk, replay-unsafe
 create is still `REQUIRED` because it duplicates.

### 4.2 Initial registry assignments

| Action | replay_semantics | durability |
|---|---|---|
| post / reply / quote (+ media variants) | `NON_IDEMPOTENT_CREATE` | `REQUIRED` |
| delete_post | `SAFE_TARGET_DELETE` | `REQUIRED` (consequential) |
| like_post | `SAFE_STATE_SET` | `BEST_EFFORT` |
| bookmark_post | `SAFE_STATE_SET` | `BEST_EFFORT` (conditional on the semantic fix, commit 1) |
| follow/unfollow (future) | `SAFE_STATE_SET` | `BEST_EFFORT` |

## 5. ApprovalGrant and EffectAttempt — two state machines

Approval and execution are distinct records. Approval state never moves
backward; execution attempts are disposable.

### 5.1 ApprovalGrant

```text
state:      ACTIVE → SPENT | EXPIRED | REVOKED
claim:      claimed_by: Optional[attempt_id]   (orthogonal lock, not a state)
bindings:   intent_hash, actor_id, action_type, target,
            policy_binding (identity/hash of the full EffectPolicy),
            authorization_epoch
validity:   expires_at; max_precommit_attempts (see 5.3)
```

- `SPENT` is terminal and irrevocable.
- `EXPIRED` by time.
- `REVOKED` by epoch bump (§7) or operator action.
- The claim lock is a compare-and-set: one attempt at a time. A proven
  `NO_EFFECT` outcome releases the claim; the grant stays `ACTIVE`.
- Grants are **ephemeral by design**: held in memory only, destroyed by
  restart. Durability is the ledger's job, not the token store's.

### 5.2 EffectAttempt

```text
PREPARING
   ├──→ NO_EFFECT              # proven: no external effect was possible
   └──→ RESERVED               # durable fence established (fenced only)
            ├──→ EFFECT_CONFIRMED
            └──→ EFFECT_UNKNOWN
```

- For non-fenced effects, `RESERVED` is absent from the persistent
  machine; the process-local permit represents the gateway boundary.
- `COMMIT_ATTEMPTED` / `submit_call_started` / `submit_call_returned`
  are **diagnostic events** in the ledger, never canonical state. A
  local process cannot atomically prove it crossed the external
  boundary across a crash; canonical states describe what WireAgent
  *knows*, not which line of control flow it probably reached.

### 5.3 Approval validity is not a retry budget

A clean precommit failure does not consume the approval, but it also
does not license an unbounded loop:

```text
ApprovalGrant.expires_at           # wall-clock validity (human-scale)
ExecutionPolicy.max_precommit_attempts   # e.g. 3
```

After `max_precommit_attempts` clean failures, automated execution
stops even though the grant may still be `ACTIVE`; a human invokes
again or approves fresh.

## 6. The unified spend rule (frozen)

> **An ApprovalGrant remains reusable after a proven `NO_EFFECT`
> preparation failure. It becomes irrevocably spent when the Commit
> Gateway grants authority capable of producing the approved external
> effect. For durability-required effects, that point is the successful
> durable `EFFECT_RESERVED` commit; for non-fenced effects, it is the
> process-local compare-and-set that activates the single-use effect
> permit.**

### 6.1 Fenced atomicity is protocol-level, not storage-level

No atomic transaction spans the filesystem ledger and an in-memory
grant object, and the design does not pretend otherwise. The rule:

> **Once `EFFECT_RESERVED` has durably committed, the approval is
> irrevocably considered spent, irrespective of whether its transient
> in-memory object was subsequently updated.**

The ledger fact dominates the transient object. Within the live
process, the gateway holds the grant's claim lock across the entire
critical section —

```text
validate → durable append → fsync → mark transient SPENT → mint permit
```

— and never releases it between those steps. After a crash the grant is
gone regardless (grants are ephemeral), and the RecoveryGuard
reconstructs the unresolved fence from the ledger.

### 6.2 Why reservation, not the browser click

Burning the approval later (at the external call) would allow a
post-reservation crash to be followed by a fresh claim of the same
approval — a second execution attempt crossing the gateway against a
reservation that exists precisely to prevent that. Spending at
reservation makes the fence self-defending. Burning earlier (at
phase-two claim) wastes human approval on failures that provably
produced no effect.

## 7. Authorization epoch

The kill switch extends from "dominates every invocation" to "revoke
outstanding execution authority":

```text
authorization_epoch: 42      # persisted with the ledger
ApprovalGrant.epoch = 42

kill() → authorization_epoch = 43
every grant with epoch 42 → invalid (REVOKED)
```

No traversal of outstanding grants is needed; validation is a
comparison. The same mechanism generalizes to any future operator-wide
authorization reset. Epoch is persisted so revocation survives
restarts.

## 8. The Commit Gateway protocol

```text
Human-approved intent
        │
        ▼
  ApprovalGrant ACTIVE
        │
        ▼
  one EffectAttempt claims it     (CAS on claimed_by)
        │
        ▼
  prepare without mutation
        (compose → validate → fill → attach → preconditions → final kill check)
        │
   ┌────┴─────────┐
   │              │
NO_EFFECT       ready
   │              │
release claim    ▼
grant stays    Commit Gateway
ACTIVE           │
          ┌──────┴────────┐
          │               │
     non-fenced       fenced
          │               │
     CAS → SPENT     durable EFFECT_RESERVED + fsync
          │               (approval irrevocably SPENT by this fact)
          └──────┬────────┘
                 ▼
         single-use EffectPermit
          (allowed_effects scoped; intent/actor/target/policy/epoch bound)
                 │
                 ▼
        scoped authority object
                 │
                 ▼
          external effect
                 │
        ┌────────┴────────┐
        ▼                 ▼
   EFFECT_CONFIRMED   EFFECT_UNKNOWN
                           │
                           ▼
               reconciliation_required (deny)
```

**Gateway invariants of operation:**

1. Every remote mutation crosses this gateway. There is no bypass
   architecture for "low-risk" actions.
2. The permit is validated and consumed atomically with the mutation
   boundary; it is single-use.
3. If a required durable reservation cannot be written, the effect
   does not occur (fail closed).
4. The kill switch is re-checked inside the gateway, immediately
   before minting/using the permit ("hand on the button", invariant 12).

## 9. Scoped authorities

After the gateway mints a permit, the capability executes against a
scoped authority object whose Python surface matches the permit's
`allowed_effects`:

```text
broker.authorize(permit) → LikeAuthority | PostAuthority | DeleteAuthority | ...
```

The concrete `WriteBroker` implements all authorities; the gateway
adapts it to the narrow surface each permit allows. This retires the
aspirational `ports.py` documentation by making it real. A capability
holding a `LikeAuthority` has no `delete()` to call — accidentally or
otherwise (§2 for the threat-model boundary of that guarantee).

## 10. Effect-knowledge states and the ledger

```text
NO_EFFECT          # proven: the effect never became possible
RESERVED           # durable fence exists; terminal outcome pending
EFFECT_CONFIRMED   # external evidence establishes the effect
EFFECT_UNKNOWN     # may have happened; retry forbidden until reconciled
```

- After restart, `RESERVED` without a terminal outcome behaves as
  `EFFECT_UNKNOWN` for enforcement. The raw ledger record is preserved;
  the *derived* recovery projection computes `unresolved = true`. Raw
  evidence is never rewritten.
- Diagnostic events (`submit_call_started`, `submit_call_returned`)
  may be recorded but carry no authority.

## 11. RecoveryGuard — enforcement, not advisory

On startup:

```text
EffectLedger → find unresolved reservations / unknown effects
            → RecoveryGuard hydrates their semantic keys
```

A matching new invocation receives, before any browser mutation is
possible:

```text
DENY — reason: reconciliation_required — effect_id: <id>
```

Surfacing unresolved effects in `health` is permitted as a convenience;
it establishes no safety property. The deny path is the guarantee.

## 12. Journal versus ledger

Two files, two contracts, two APIs:

```text
.webwire/journal.ndjson   — audit/diagnostic; append_best_effort();
                            I/O failures logged, never block execution
.webwire/effects.ndjson   — safety-critical; append_durable();
                            fsync-backed; failure to write a REQUIRED
                            reservation blocks the effect
```

Invariant 15 is rewritten accordingly:

> **The durable effect ledger is the authority for executed, uncertain,
> and reserved external effects and for rebuilding effect-level dedupe.
> The invocation journal is an audit/diagnostic record and is not relied
> upon for commit safety.**

## 13. The twelve frozen invariants

1. Every remote mutation crosses one Commit Gateway.
2. Every execution authority is scoped to the approved semantic effect.
3. Human approval and execution attempts are distinct records.
4. Clean, proven precommit failure does not consume human approval.
5. Only one execution attempt may claim an approval at once.
6. For durability-required effects, successful reservation atomically
   spends the approval before external mutation (per §6.1's
   protocol-level definition).
7. No external mutation occurs if a required durable reservation fails.
8. Any unresolved reservation blocks semantic replay after restart.
9. Unknown external outcome is never automatically retried.
10. Risk, authority, and delivery semantics are independent policy
    dimensions.
11. Kill-switch activation invalidates outstanding execution authority
    (authorization epoch).
12. Verification and reconciliation report evidence, not inferred
    success.

## 14. Acceptance tests (T1–T14)

Fault-injection tests are the acceptance criteria; they land with the
layer they exercise, not after.

| # | Scenario | Required outcome |
|---|---|---|
| T1 | Reservation fsync fails | Irreversible effect MUST NOT occur |
| T2 | Crash after reservation, before commit | Restart → `reconciliation_required`; no auto-submit |
| T3 | Crash immediately after submit | Restart → `reconciliation_required`; no duplicate submit |
| T4 | External submit returns timeout | `EFFECT_UNKNOWN`; dedupe immediately blocks retry |
| T5 | Verification fails after observed effect | Effect remains recorded; no retry |
| T6 | LikeCapability attempts `delete()` | Authority surface rejects before browser interaction |
| T7 | Permit for target A used on target B | Commit gateway rejects |
| T8 | Permit reuse | Rejected |
| T9 | Permit expires before commit | Rejected |
| T10 | Actor changes between approval and commit | Rejected |
| T11 | Intent payload/media changes after approval | Rejected (existing hash-mismatch behavior, now bound to the gateway) |
| T12 | Audit journal write fails after successful commit | Safety ledger remains authoritative |
| T13 | Clean precommit failure preserves approval | Claim released; grant ACTIVE; same approval funds another attempt |
| T14 | Reservation consumes approval | Post-reservation crash; same approval cannot be reclaimed; matching intent blocked by RecoveryGuard |

## 15. Build order

```text
1. EffectPolicy + EffectLedger primitives
2. ApprovalGrant / EffectAttempt models
3. Commit Gateway
4. Scoped authorities
5. Capability migration through the gateway
6. RecoveryGuard
7. Journal becomes audit-only (invariant 15 rewrite lands here)
```

EffectPolicy precedes the gateway because the gateway's first question
— does this effect require durable fencing, and what verb is permitted —
is answered by the policy. The adversarial tests land with each layer.
