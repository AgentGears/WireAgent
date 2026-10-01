# M8 — User Rule Layer Design

```text
Status:   FROZEN FOR IMPLEMENTATION
Baseline: main 65e3961ed32d0908a47983901d153838cf99267
Change rule: preserve M5/M6 semantics untouched; revise only when
implementation, fault injection, or live evidence falsifies an assumption.
```

This document is normative and self-contained. Build M8 from this file, not
from conversation memory. `docs/M5_DESIGN.md` remains authoritative for
effect-transaction semantics; `docs/M6_DESIGN.md` for reconciliation and
qualification. M8 does not reopen either contract. M8 does not depend on M7.

## 1. Problem

Every write today requires a per-invocation human confirmation. That is the
correct default and it does not change. What is missing is the standing
decision: the owner who has already decided, once and in their own words,
that a class of actions is always fine (stop asking), always forbidden
(never do), or always worth a question (keep asking — today's behavior).

M8 adds exactly that: user-written rules, compiled once into structured
selectors, enforced deterministically at the existing approval gate.

The design was informed by the consumer personal-agent approval patterns
surveyed on 2026-09-30 (rule cards, plain-word rules, ask-first precedence).
It deliberately diverges from that pattern in one respect: no model runs in
the enforcement path, ever. A model may assist at rule-CREATION time only,
with its output confirmed by the human before anything is stored.

## 2. What M8 does not guarantee (negative guarantees)

- **No runtime model in enforcement.** Rule matching is deterministic set
  logic over structured intents. The compiler (layer 3) is the only model
  surface, runs once per rule with human confirmation, and is optional.
- **No auto-approval above the risk-tier ceiling.** Standing ALLOW rules can
  auto-approve only `PRIVATE_REVERSIBLE` and
  `PUBLIC_REVERSIBLE_ENGAGEMENT` actions. Anything higher asks, regardless
  of what a rule says. There is no per-rule override of the ceiling.
- **No durability changes.** The effect ledger, fencing, replay semantics,
  and recovery are untouched. Rules are policy configuration, not effects;
  they never enter the ledger.
- **No grant survives on borrowed authority.** A rule-granted approval binds
  the same policy hash, actor, target, and epoch as a human-granted one, and
  dies identically on policy drift or an epoch bump.
- **Fail-open means "everything asks."** A missing, corrupt, or unreadable
  rule store yields zero active rules — which is exactly today's behavior.
  No rule failure can widen what a rule permits.

## 3. Terminology

| Term | Meaning |
|---|---|
| **UserRule** | One standing decision: a selector, a decision, provenance, a TTL. |
| **RuleSelector** | The structured scope: which actions/targets/actors/tiers the rule applies to. All specified dimensions must match; unspecified dimensions match anything. |
| **RuleDecision** | `allow` (auto-approve), `ask` (confirmation card, today's behavior), `never` (deny before token issuance). |
| **Ceiling** | The registry-fixed tier boundary above which ALLOW cannot apply. |
| **Approver attribution** | Grants and ledger records cite `rule:<id>` when a rule, not a human card, supplied the approval. |

## 4. The selector model (layer 1)

```text
RuleSelector
├── action_types: frozenset[str] | None     # base action types, e.g. {"bookmark"}
├── risk_tiers:   frozenset[RiskTier] | None
├── target_types: frozenset[str] | None     # e.g. {"post"}, {"user"}
├── target_ids:   frozenset[str] | None     # post ids / handles
└── actors:       frozenset[str] | None     # resolved handles

matches(action_type, risk_tier, target_type, target_id, actor) -> bool
```

Every specified dimension must match. A selector with only `action_types`
set applies to that action for any target and actor. An empty selector
(no dimensions) is REJECTED at creation: a rule must name its scope.

```text
UserRule
├── rule_id, selector, decision
├── created_at, expires_at          # TTL mandatory; default 7 days
├── provenance: "hand_written" | "compiled"
└── source_text                     # the owner's words, kept for re-confirmation display
```

### 4.1 Store contract (amended after the PR #20 fallback review, F-01..F-07)

The external reviewer quota was unavailable; a first-pass plus adversarial
fallback review found seven defects, three blocking. The amended contract:

- **Whole-read invalidation.** Any invalid persisted entry — or duplicate
  rule ids — voids the ENTIRE read to zero rules. A policy document
  containing an invalid entry is never partially trusted: skipping one
  entry could drop a restrictive NEVER while a permissive ALLOW stays live.
- **Fresh read per match.** No enforcement caching. Revocation freshness is
  a store invariant: a deleted ALLOW or a newly added NEVER must be
  observed by the very next match in every process.
- **Finite, forward TTLs.** NaN and ±inf (which JSON accepts as literals)
  and expires_at <= created_at are rejected at construction and at parse.
- **Persistence failures raise** (`RuleStoreError`), never log-and-continue.
- **Strict parse.** Every mandatory field — including provenance and
  source_text — must be present and well-typed. Provenance is never
  manufactured during deserialization.
- **Ids are non-empty strings, unique within a store.** save() refuses
  duplicates at the write boundary.
- **Each save stages through a unique per-writer temporary file** (F-08,
  post-merge review). A shared staging file let one writer's replace install
  another writer's bytes while the first reported success. Unique staging
  gives concurrent whole-store saves last-writer-wins linearization; a
  bounded retry absorbs the Windows transient sharing violation on the
  destination.
- **Selector annotations are runtime contracts.** Every specified dimension
  must be a non-empty frozenset of the exact element type (strings;
  RiskTier enums for risk_tiers); empty dimensions are rejected.
- **Timestamp parity.** Construction and parsing reject the same values:
  bools (numeric in Python), NaN, ±inf, and expires_at <= created_at.

## 5. Matching and precedence (deterministic)

At the gate, the kernel holds the structured intent. Matching is dictionary
lookup. When multiple rules match one intent:

```text
never  >  ask  >  allow          (the strict order; ask-first wins conflicts)
```

The ceiling is applied as a MATCH-TIME downgrade, not a creation-time
restriction: an ALLOW rule that matches an action above the ceiling yields
`ask`. This keeps creation simple (the owner can write "allow posting" and
the system honestly reports "that will still ask") and makes the ceiling
impossible to bypass by any store content.

## 6. The three-way gate (layer 2)

> **Amended after the PR #20 post-merge review.** The original wording had
> the rule gate "mint ApprovalGrant with approver=rule:<id>" — creating a
> second grant-mint seam inside WriteKernel. The kernel does not own grant
> creation: it owns the human confirmation carrier, validates and consumes
> it, and invokes the execution runtime, which is the SINGLE existing
> ApprovalGrant mint authority. The implementation topology falsified the
> original assumption; M8's change rule applies.

The corrected contract — **one execution-authority mint seam, not two**:

```text
preview
  ↓
rule gate (in WriteKernel, before the confirmation gate)
  ├─ NEVER → DENY; blocked_by=user_rule; rule cited; no confirmation authority
  │
  ├─ ASK / no match
  │     → existing human-confirmation flow (unchanged)
  │     → successful confirmation establishes approver="human"
  │
  └─ ALLOW
        → no human confirmation carrier minted
        → establishes approver="rule:<id>"
        ↓
existing execution path (unchanged)
        ↓
single existing ApprovalGrant mint seam (the execution runtime)
        ↓
grant carries immutable approver attribution
```

**The rule gate is re-evaluated on EVERY invocation, including the
confirmation-token invocation.** The kernel already recomposes and previews
on the second invocation before validating the token, so a newly installed
NEVER defeats a previously issued human token — the rule gate dominates
tokens, matching the frozen precedence that standing policy outranks stale
authority.

### 6.1 Attribution descends monotonically through the authority lineage

```text
Rule/Human decision
    ↓
ApprovalGrant.approver
    ↓
EffectPermit.approver
    ↓
EffectLedgerRecord.approver
```

- `ApprovalGain.approver` — the grant reuses the existing M5 lifecycle and
  bindings (intent, actor, action, target, policy binding, epoch); T10/T11
  reuse the existing policy-mismatch and epoch denials rather than new ones.
- `EffectPermit.approver` — the permit is the immutable carrier across the
  boundary where grants may be pruned while terminal evidence is written
  later.
- `EffectLedgerRecord.approver` — a TOP-LEVEL lineage field (beside
  semantic key, action, intent, policy, actor, target), immutable across
  reservation and terminal records. Optional on deserialization for pre-M8
  rows; every M8-created record supplies it.

## 7. The compiler (layer 3)

Natural language in; a structured selector out; the compiled form shown back
to the owner in plain words for confirmation before storage. Clauses the
compiler cannot express deterministically are REJECTED with an explanation,
never fuzzified.

- The model is accessed with a plain API key from configuration.
- The compiler is optional and pluggable: hand-written rules work with no
  model configured at all.
- Compile-time only. No model executes during matching, gating, or approval.
- The owner confirms the compilation, not just the words. Ambiguity surfaces
  once, at creation, not on every action.

## 8. The surface (layer 4)

A minimal card-style interface over the existing phase-1/phase-2 flow: it
renders the preview as a card (summary, warnings, matched rule), takes
approve/deny, and holds the confirmation token internally. The CLI scope is
exactly what cards need — this is not a workflow framework.

## 9. Build order

```text
1. Rule store + selector model + deterministic matcher + ceiling   (this PR)
2. Kernel three-way gate + approver attribution
3. Compiler (API-key, pluggable) + confirm-compile surface
4. Card CLI + rule lifecycle surface (list, TTL re-confirm)
```

Qualification follows each layer in the established fault-injection style.

## 10. Acceptance tests

| # | Scenario | Required outcome |
|---|---|---|
| M8-T1 | ALLOW rule matches below-ceiling action | decision allow, rule cited |
| M8-T2 | ALLOW rule matches above-ceiling action | downgraded to ask |
| M8-T3 | NEVER and ALLOW both match | never wins; no token |
| M8-T4 | ASK and ALLOW both match | ask wins |
| M8-T5 | No rule matches | ask (today's default) |
| M8-T6 | Rule TTL expired | rule does not match |
| M8-T7 | Corrupt/missing store | zero active rules; everything asks |
| M8-T8 | Selector dimension mismatch | no match |
| M8-T9 | Empty selector | rejected at creation |
| M8-T10 | Rule-granted approval under policy drift | grant denied (existing binding validation) |
| M8-T11 | Rule-granted approval after epoch bump | grant dead (existing epoch rule) |
| M8-T12 | Compiler rejects inexpressible clause | stored nothing; explanation surfaced |
| M8-T13 | Matching NEVER, no token supplied | deny, rule cited, no token minted |
| M8-T14 | Matching NEVER, valid old human token supplied | still deny — rule gate dominates tokens |
| M8-T15 | Matching ASK | ordinary confirmation path; matched rule cited |
| M8-T16 | No rule matches | authorization behavior byte-for-byte identical to pre-M8 |
| M8-T17 | Below-ceiling ALLOW | no human token; execution grant carries approver=rule:id |
| M8-T18 | Above-ceiling ALLOW | ASK with ceiling_downgraded, rule cited |
| M8-T19 | Corrupt/missing store at gate time | human confirmation; never auto-allow |
| M8-T20 | Rule ALLOW, policy changes before commit | existing policy_mismatch denial |
| M8-T21 | Rule ALLOW, epoch advances before commit | existing epoch denial |
| M8-T22 | Rule-granted reservation + terminal record | identical approver on both durable records |
| M8-T23 | Pre-M8 ledger history | still parses and recovers (approver optional on read) |
| M8-T24 | Human-confirmed execution | grant/permit/ledger attribution says human, never rule-derived |
