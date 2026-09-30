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

Slotted between preview and token issuance in the kernel pipeline:

```text
compose → registry gate → budget → dedupe → preview
       → RULE GATE:
            never  → DENY  (blocked_by=user_rule, rule_id cited) — no token minted
            allow  → mint ApprovalGrant with approver="rule:<id>"
                     (tier ceiling already downgraded any above-ceiling match to ask)
            ask    → confirmation_required (today's behavior), card cites the rule
            no match → confirmation_required (today's default)
       → gateway → ledger → fencing → spend → recovery     [ALL UNCHANGED]
```

Layer 2 touches three things: the gate itself, an `approver` field on
`ApprovalGrant`, and an `approver` citation on ledger records. Nothing else
below the surface changes.

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
